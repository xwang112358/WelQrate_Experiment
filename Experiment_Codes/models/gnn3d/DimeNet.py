from torch.nn import SiLU as swish
from torch_geometric.nn.models.dimenet import (
    BesselBasisLayer,
    Envelope,
    ResidualLayer,
    SphericalBasisLayer,
)
import sympy as sym
import torch
from torch_geometric.nn.inits import glorot_orthogonal
from torch_scatter import scatter
from torch_sparse import SparseTensor
from torch_geometric.nn import radius_graph
from torch import nn
from torch.nn import Linear, ReLU, Dropout
from math import sqrt
from torch_geometric.nn.resolver import activation_resolver
from torch.nn import Embedding, Linear, BatchNorm1d, Sequential


class FeatureEmbedding(torch.nn.Module):
    def __init__(self, input_dim, output_dim):
        super(FeatureEmbedding, self).__init__()
        self.network = Sequential(
            Linear(input_dim, output_dim // 2),
            BatchNorm1d(output_dim // 2),
            ReLU(),
            Linear(output_dim // 2, output_dim),
            ReLU()
        )
    
    def forward(self, x):
        return self.network(x)
    
    def reset_parameters(self):
        for module in self.network:
            if hasattr(module, 'reset_parameters'):
                module.reset_parameters()


class EmbeddingBlock(torch.nn.Module):
    def __init__(self, num_radial: int, hidden_channels: int, act, one_hot = False):
        super().__init__()
        self.act = act

        if one_hot:
            self.emb = Embedding(100, hidden_channels)
        else: 
            # rich features has 28 dimensions, check x in dataset
            self.emb = FeatureEmbedding(28, hidden_channels)
        self.lin_rbf = Linear(num_radial, hidden_channels)
        self.lin = Linear(3 * hidden_channels, hidden_channels)

        self.reset_parameters()

    def reset_parameters(self):
        self.emb.reset_parameters()
        self.lin_rbf.reset_parameters()
        self.lin.reset_parameters()

    def forward(self, x, rbf, i, j):
        x = self.emb(x)
        rbf = self.act(self.lin_rbf(rbf))
        return self.act(self.lin(torch.cat([x[i], x[j], rbf], dim=-1)))



class MLP(torch.nn.Module):
    def __init__(self, input_size, output_size, hidden_sizes, activation_hidden, activation_out, biases, dropout):
        super(MLP, self).__init__()
        self.activation_hidden = activation_hidden
        self.activation_out = activation_out
        self.dropout = dropout

        if len(hidden_sizes) > 0:
            self.linear_layers = torch.nn.ModuleList([torch.nn.Linear(input_size, hidden_sizes[0], bias = biases)])
            self.linear_layers.extend([torch.nn.Linear(in_size, out_size, bias = biases) 
                                       for (in_size, out_size) 
                                       in zip(hidden_sizes[0:-1], (hidden_sizes[1:]))])
            self.linear_layers.append(torch.nn.Linear(hidden_sizes[-1], output_size, bias = biases))
        
        else:
            self.linear_layers = torch.nn.ModuleList([torch.nn.Linear(input_size, output_size, bias = biases)])
        
    def forward(self, x):
        if len(self.linear_layers) == 1:
            out = self.activation_out(self.linear_layers[0](x))
        
        else:
            out = self.activation_hidden(self.linear_layers[0](x))
            for i, layer in enumerate(self.linear_layers[1:-1]):
                out = self.activation_hidden(layer(out))
                out = torch.nn.functional.dropout(out, p = self.dropout, training = self.training)
            out = self.activation_out(self.linear_layers[-1](out))

        return out
    
class InteractionBlock(torch.nn.Module):
    def __init__(
        self,
        hidden_channels: int,
        num_bilinear: int,
        num_spherical: int,
        num_radial: int,
        num_before_skip: int,
        num_after_skip: int,
        act,
    ):
        super().__init__()
        self.act = act

        self.lin_rbf = Linear(num_radial, hidden_channels, bias=False)
        self.lin_sbf = Linear(num_spherical * num_radial, num_bilinear,
                              bias=False)

        # Dense transformations of input messages.
        self.lin_kj = Linear(hidden_channels, hidden_channels)
        self.lin_ji = Linear(hidden_channels, hidden_channels)

        self.W = torch.nn.Parameter(
            torch.empty(hidden_channels, num_bilinear, hidden_channels))

        self.layers_before_skip = torch.nn.ModuleList([
            ResidualLayer(hidden_channels, act) for _ in range(num_before_skip)
        ])
        self.lin = Linear(hidden_channels, hidden_channels)
        self.layers_after_skip = torch.nn.ModuleList([
            ResidualLayer(hidden_channels, act) for _ in range(num_after_skip)
        ])

        self.reset_parameters()

    def reset_parameters(self):
        glorot_orthogonal(self.lin_rbf.weight, scale=2.0)
        glorot_orthogonal(self.lin_sbf.weight, scale=2.0)
        glorot_orthogonal(self.lin_kj.weight, scale=2.0)
        self.lin_kj.bias.data.fill_(0)
        glorot_orthogonal(self.lin_ji.weight, scale=2.0)
        self.lin_ji.bias.data.fill_(0)
        self.W.data.normal_(mean=0, std=2 / self.W.size(0))
        for res_layer in self.layers_before_skip:
            res_layer.reset_parameters()
        glorot_orthogonal(self.lin.weight, scale=2.0)
        self.lin.bias.data.fill_(0)
        for res_layer in self.layers_after_skip:
            res_layer.reset_parameters()

    def forward(self, x, rbf, sbf, idx_kj, idx_ji):
        rbf = self.lin_rbf(rbf)
        sbf = self.lin_sbf(sbf)

        x_ji = self.act(self.lin_ji(x))
        x_kj = self.act(self.lin_kj(x))
        x_kj = x_kj * rbf
        x_kj = torch.einsum('wj,wl,ijl->wi', sbf, x_kj[idx_kj], self.W)
        x_kj = scatter(x_kj, idx_ji, dim=0, dim_size=x.size(0), reduce='sum')

        h = x_ji + x_kj
        for layer in self.layers_before_skip:
            h = layer(h)
        h = self.act(self.lin(h)) + x
        for layer in self.layers_after_skip:
            h = layer(h)

        return h

class InteractionPPBlock(torch.nn.Module):
    def __init__(
        self,
        hidden_channels: int,
        int_emb_size: int,
        basis_emb_size: int,
        num_spherical: int,
        num_radial: int,
        num_before_skip: int,
        num_after_skip: int,
        act,
    ):
        super().__init__()
        self.act = act

        # Transformation of Bessel and spherical basis representations:
        self.lin_rbf1 = Linear(num_radial, basis_emb_size, bias=False)
        self.lin_rbf2 = Linear(basis_emb_size, hidden_channels, bias=False)

        self.lin_sbf1 = Linear(num_spherical * num_radial, basis_emb_size,
                               bias=False)
        self.lin_sbf2 = Linear(basis_emb_size, int_emb_size, bias=False)

        # Hidden transformation of input message:
        self.lin_kj = Linear(hidden_channels, hidden_channels)
        self.lin_ji = Linear(hidden_channels, hidden_channels)

        # Embedding projections for interaction triplets:
        self.lin_down = Linear(hidden_channels, int_emb_size, bias=False)
        self.lin_up = Linear(int_emb_size, hidden_channels, bias=False)

        # Residual layers before and after skip connection:
        self.layers_before_skip = torch.nn.ModuleList([
            ResidualLayer(hidden_channels, act) for _ in range(num_before_skip)
        ])
        self.lin = Linear(hidden_channels, hidden_channels)
        self.layers_after_skip = torch.nn.ModuleList([
            ResidualLayer(hidden_channels, act) for _ in range(num_after_skip)
        ])

        self.reset_parameters()

    def reset_parameters(self):
        glorot_orthogonal(self.lin_rbf1.weight, scale=2.0)
        glorot_orthogonal(self.lin_rbf2.weight, scale=2.0)
        glorot_orthogonal(self.lin_sbf1.weight, scale=2.0)
        glorot_orthogonal(self.lin_sbf2.weight, scale=2.0)

        glorot_orthogonal(self.lin_kj.weight, scale=2.0)
        self.lin_kj.bias.data.fill_(0)
        glorot_orthogonal(self.lin_ji.weight, scale=2.0)
        self.lin_ji.bias.data.fill_(0)

        glorot_orthogonal(self.lin_down.weight, scale=2.0)
        glorot_orthogonal(self.lin_up.weight, scale=2.0)

        for res_layer in self.layers_before_skip:
            res_layer.reset_parameters()
        glorot_orthogonal(self.lin.weight, scale=2.0)
        self.lin.bias.data.fill_(0)
        for res_layer in self.layers_after_skip:
            res_layer.reset_parameters()

    def forward(self, x, rbf, sbf, idx_kj, idx_ji):
        # Initial transformation:
        x_ji = self.act(self.lin_ji(x))
        x_kj = self.act(self.lin_kj(x))

        # Transformation via Bessel basis:
        rbf = self.lin_rbf1(rbf)
        rbf = self.lin_rbf2(rbf)
        x_kj = x_kj * rbf

        # Down project embedding and generating triple-interactions:
        x_kj = self.act(self.lin_down(x_kj))

        # Transform via 2D spherical basis:
        sbf = self.lin_sbf1(sbf)
        sbf = self.lin_sbf2(sbf)
        x_kj = x_kj[idx_kj] * sbf

        # Aggregate interactions and up-project embeddings:
        x_kj = scatter(x_kj, idx_ji, dim=0, dim_size=x.size(0), reduce='sum')
        x_kj = self.act(self.lin_up(x_kj))

        h = x_ji + x_kj
        for layer in self.layers_before_skip:
            h = layer(h)
        h = self.act(self.lin(h)) + x
        for layer in self.layers_after_skip:
            h = layer(h)

        return h
    
def triplets(edge_index, num_nodes):
    row, col = edge_index  # j->i

    value = torch.arange(row.size(0), device=row.device)
    adj_t = SparseTensor(row=col, col=row, value=value,
                         sparse_sizes=(num_nodes, num_nodes))
    adj_t_row = adj_t[row]
    num_triplets = adj_t_row.set_value(None).sum(dim=1).to(torch.long)

    # Node indices (k->j->i) for triplets.
    idx_i = col.repeat_interleave(num_triplets)
    idx_j = row.repeat_interleave(num_triplets)
    idx_k = adj_t_row.storage.col()
    mask = idx_i != idx_k  # Remove i == k triplets.
    idx_i, idx_j, idx_k = idx_i[mask], idx_j[mask], idx_k[mask]

    # Edge indices (k-j, j->i) for triplets.
    idx_kj = adj_t_row.storage.value()[mask]
    idx_ji = adj_t_row.storage.row()[mask]

    return col, row, idx_i, idx_j, idx_k, idx_kj, idx_ji

class OutputBlock(torch.nn.Module):
    def __init__(
        self,
        num_radial: int,
        hidden_channels: int,
        out_channels: int,
        num_layers: int,
        act,
        output_initializer: str = 'zeros',
    ):
        assert output_initializer in {'zeros', 'glorot_orthogonal'}

        super().__init__()

        self.act = act
        self.output_initializer = output_initializer

        self.lin_rbf = Linear(num_radial, hidden_channels, bias=False)
        self.lins = torch.nn.ModuleList()
        for _ in range(num_layers):
            self.lins.append(Linear(hidden_channels, hidden_channels))
        self.lin = Linear(hidden_channels, out_channels, bias=False)

        self.reset_parameters()

    def reset_parameters(self):
        glorot_orthogonal(self.lin_rbf.weight, scale=2.0)
        for lin in self.lins:
            glorot_orthogonal(lin.weight, scale=2.0)
            lin.bias.data.fill_(0)
        if self.output_initializer == 'zeros':
            self.lin.weight.data.fill_(0)
        elif self.output_initializer == 'glorot_orthogonal':
            glorot_orthogonal(self.lin.weight, scale=2.0)

    def forward(self, x, rbf, i, num_nodes):
        x = self.lin_rbf(rbf) * x
        x = scatter(x, i, dim=0, dim_size=num_nodes, reduce='sum')
        for lin in self.lins:
            x = self.act(lin(x))
            
        return self.lin(x)
    

class OutputPPBlock(torch.nn.Module):
    def __init__(
        self,
        num_radial: int,
        hidden_channels: int,
        out_emb_channels: int,
        out_channels: int,
        num_layers: int,
        act,
        output_initializer: str = 'zeros',
    ):
        assert output_initializer in {'zeros', 'glorot_orthogonal'}

        super().__init__()

        self.act = act
        self.output_initializer = output_initializer

        self.lin_rbf = Linear(num_radial, hidden_channels, bias=False)

        # The up-projection layer:
        self.lin_up = Linear(hidden_channels, out_emb_channels, bias=False)
        self.lins = torch.nn.ModuleList()
        for _ in range(num_layers):
            self.lins.append(Linear(out_emb_channels, out_emb_channels))
        self.lin = Linear(out_emb_channels, out_channels, bias=False)

        self.reset_parameters()

    def reset_parameters(self):
        glorot_orthogonal(self.lin_rbf.weight, scale=2.0)
        glorot_orthogonal(self.lin_up.weight, scale=2.0)
        for lin in self.lins:
            glorot_orthogonal(lin.weight, scale=2.0)
            lin.bias.data.fill_(0)
        if self.output_initializer == 'zeros':
            self.lin.weight.data.fill_(0)
        elif self.output_initializer == 'glorot_orthogonal':
            glorot_orthogonal(self.lin.weight, scale=2.0)

    def forward(self, x, rbf, i, num_nodes = None):
        x = self.lin_rbf(rbf) * x
        x = scatter(x, i, dim=0, dim_size=num_nodes, reduce='sum')
        x = self.lin_up(x)
        for lin in self.lins:
            x = self.act(lin(x))
        return self.lin(x)
    

class DimeNet(torch.nn.Module):
    r"""The directional message passing neural network (DimeNet) from the
    `"Directional Message Passing for Molecular Graphs"
    <https://arxiv.org/abs/2003.03123>`_ paper.
    DimeNet transforms messages based on the angle between them in a
    rotation-equivariant fashion.

    .. note::

        For an example of using a pretrained DimeNet variant, see
        `examples/qm9_pretrained_dimenet.py
        <https://github.com/pyg-team/pytorch_geometric/blob/master/examples/
        qm9_pretrained_dimenet.py>`_.

    Args:
        hidden_channels (int): Hidden embedding size.
        out_channels (int): Size of each output sample.
        num_blocks (int): Number of building blocks.
        num_bilinear (int): Size of the bilinear layer tensor.
        num_spherical (int): Number of spherical harmonics.
        num_radial (int): Number of radial basis functions.
        cutoff (float, optional): Cutoff distance for interatomic
            interactions. (default: :obj:`5.0`)
        max_num_neighbors (int, optional): The maximum number of neighbors to
            collect for each node within the :attr:`cutoff` distance.
            (default: :obj:`32`)
        envelope_exponent (int, optional): Shape of the smooth cutoff.
            (default: :obj:`5`)
        num_before_skip (int, optional): Number of residual layers in the
            interaction blocks before the skip connection. (default: :obj:`1`)
        num_after_skip (int, optional): Number of residual layers in the
            interaction blocks after the skip connection. (default: :obj:`2`)
        num_output_layers (int, optional): Number of linear layers for the
            output blocks. (default: :obj:`3`)
        act (str or Callable, optional): The activation function.
            (default: :obj:`"swish"`)
        output_initializer (str, optional): The initialization method for the
            output layer (:obj:`"zeros"`, :obj:`"glorot_orthogonal"`).
            (default: :obj:`"zeros"`)
    """

    url = ('https://github.com/klicperajo/dimenet/raw/master/pretrained/'
           'dimenet')

    def __init__(
        self,
        hidden_channels: int,
        out_channels: int,
        num_blocks: int,
        num_bilinear: int,
        num_spherical: int,
        num_radial: int,
        cutoff: float = 6.0,
        max_num_neighbors: int = 50,
        envelope_exponent: int = 5,
        num_before_skip: int = 1,
        num_after_skip: int = 2,
        num_output_layers: int = 3,
        act: str = 'swish',
        output_initializer: str = 'zeros',
        one_hot = False
    ):
        super().__init__()

        if num_spherical < 2:
            raise ValueError("'num_spherical' should be greater than 1")

        act = activation_resolver(act)

        self.cutoff = cutoff
        self.one_hot = one_hot
        self.max_num_neighbors = max_num_neighbors
        self.num_blocks = num_blocks

        self.rbf = BesselBasisLayer(num_radial, cutoff, envelope_exponent)
        self.sbf = SphericalBasisLayer(num_spherical, num_radial, cutoff,
                                       envelope_exponent)

        # may change this 
        self.emb = EmbeddingBlock(num_radial, hidden_channels, act, one_hot)

        self.output_blocks = torch.nn.ModuleList([
            OutputBlock(
                num_radial,
                hidden_channels,
                out_channels,
                num_output_layers,
                act,
                output_initializer,
            ) for _ in range(num_blocks + 1)
        ])

        self.interaction_blocks = torch.nn.ModuleList([
            InteractionBlock(
                hidden_channels,
                num_bilinear,
                num_spherical,
                num_radial,
                num_before_skip,
                num_after_skip,
                act,
            ) for _ in range(num_blocks)
        ])

    def reset_parameters(self):
        r"""Resets all learnable parameters of the module."""
        self.rbf.reset_parameters()
        self.emb.reset_parameters()
        for out in self.output_blocks:
            out.reset_parameters()
        for interaction in self.interaction_blocks:
            interaction.reset_parameters()

    def forward(self, batch_data):
        r"""Forward pass.

        Args:
            z (torch.Tensor): Atomic number of each atom with shape
                :obj:`[num_atoms]`.
            pos (torch.Tensor): Coordinates of each atom with shape
                :obj:`[num_atoms, 3]`.
            batch (torch.Tensor, optional): Batch indices assigning each atom
                to a separate molecule with shape :obj:`[num_atoms]`.
                (default: :obj:`None`)
        """
        # use the pre-calculated edge_index in the dataset
        x, pos, batch, edge_index = batch_data.x, batch_data.pos, batch_data.batch, batch_data.edge_index
        # edge_index = radius_graph(pos, r=self.cutoff, batch=batch,
        #                           max_num_neighbors=self.max_num_neighbors)
        z = torch.argmax(batch_data.one_hot_atom, dim = 1)
        
        # print(z.shape)
        i, j, idx_i, idx_j, idx_k, idx_kj, idx_ji = triplets(
            edge_index, num_nodes=z.size(0))

        # Calculate distances.
        dist = (pos[i] - pos[j]).pow(2).sum(dim=-1).sqrt()

        # ####################Calculate angles.####################
        # if isinstance(self, DimeNetPlusPlus):
        #     pos_jk, pos_ij = pos[idx_j] - pos[idx_k], pos[idx_i] - pos[idx_j]
        #     a = (pos_ij * pos_jk).sum(dim=-1)
        #     b = torch.cross(pos_ij, pos_jk, dim=1).norm(dim=-1)
        # elif isinstance(self, DimeNet):
        pos_ji, pos_ki = pos[idx_j] - pos[idx_i], pos[idx_k] - pos[idx_i]
        a = (pos_ji * pos_ki).sum(dim=-1)
        b = torch.cross(pos_ji, pos_ki, dim=1).norm(dim=-1)
        ####################


        angle = torch.atan2(b, a)

        rbf = self.rbf(dist)
        sbf = self.sbf(dist, angle, idx_kj)

        # Embedding block for one hot encoding of atomic numbers.
        if self.one_hot:
            x = self.emb(z, rbf, i, j)
        else:
            x = self.emb(x, rbf, i, j) # need change
        
    
        P = self.output_blocks[0](x, rbf, i, num_nodes=pos.size(0))

        # Interaction blocks.
        for interaction_block, output_block in zip(self.interaction_blocks,
                                                   self.output_blocks[1:]):
            x = interaction_block(x, rbf, sbf, idx_kj, idx_ji)
            P = P + output_block(x, rbf, i, num_nodes=pos.size(0))

        if batch is None:
            return P.sum(dim=0)
        else:
            return scatter(P, batch, dim=0, reduce='sum')
        

class DimeNetPlusPlus(DimeNet):
    r"""The DimeNet++ from the `"Fast and Uncertainty-Aware
    Directional Message Passing for Non-Equilibrium Molecules"
    <https://arxiv.org/abs/2011.14115>`_ paper.

    :class:`DimeNetPlusPlus` is an upgrade to the :class:`DimeNet` model with
    8x faster and 10% more accurate than :class:`DimeNet`.

    Args:
        hidden_channels (int): Hidden embedding size.
        out_channels (int): Size of each output sample.
        num_blocks (int): Number of building blocks.
        int_emb_size (int): Size of embedding in the interaction block.
        basis_emb_size (int): Size of basis embedding in the interaction block.
        out_emb_channels (int): Size of embedding in the output block.
        num_spherical (int): Number of spherical harmonics.
        num_radial (int): Number of radial basis functions.
        cutoff: (float, optional): Cutoff distance for interatomic
            interactions. (default: :obj:`6.0`)
        max_num_neighbors (int, optional): The maximum number of neighbors to
            collect for each node within the :attr:`cutoff` distance.
            (default: :obj:`32`)
        envelope_exponent (int, optional): Shape of the smooth cutoff.
            (default: :obj:`5`)
        num_before_skip: (int, optional): Number of residual layers in the
            interaction blocks before the skip connection. (default: :obj:`1`)
        num_after_skip: (int, optional): Number of residual layers in the
            interaction blocks after the skip connection. (default: :obj:`2`)
        num_output_layers: (int, optional): Number of linear layers for the
            output blocks. (default: :obj:`3`)
        act: (str or Callable, optional): The activation funtion.
            (default: :obj:`"swish"`)
        output_initializer (str, optional): The initialization method for the
            output layer (:obj:`"zeros"`, :obj:`"glorot_orthogonal"`).
            (default: :obj:`"zeros"`)
    """

    url = ('https://raw.githubusercontent.com/gasteigerjo/dimenet/'
           'master/pretrained/dimenet_pp')

    def __init__(
        self,
        hidden_channels: int,
        out_channels: int,
        num_blocks: int,
        int_emb_size: int,
        basis_emb_size: int,
        out_emb_channels: int,
        num_spherical: int,
        num_radial: int,
        cutoff: float = 6.0,
        max_num_neighbors: int = 50,
        envelope_exponent: int = 5,
        num_before_skip: int = 1,
        num_after_skip: int = 2,
        num_output_layers: int = 3,
        act = 'swish',
        output_initializer = 'zeros',
        one_hot = False
    ):
        act = activation_resolver(act)

        super().__init__(
            hidden_channels=hidden_channels,
            out_channels=out_channels,
            num_blocks=num_blocks,
            num_bilinear=1,
            num_spherical=num_spherical,
            num_radial=num_radial,
            cutoff=cutoff,
            max_num_neighbors=max_num_neighbors,
            envelope_exponent=envelope_exponent,
            num_before_skip=num_before_skip,
            num_after_skip=num_after_skip,
            num_output_layers=num_output_layers,
            act=act,
            output_initializer=output_initializer,
            one_hot=one_hot
        )

        # We are re-using the RBF, SBF and embedding layers of `DimeNet` and
        # redefine output_block and interaction_block in DimeNet++.
        # Hence, it is to be noted that in the above initalization, the
        # variable `num_bilinear` does not have any purpose as it is used
        # solely in the `OutputBlock` of DimeNet:
        self.output_blocks = torch.nn.ModuleList([
            OutputPPBlock(
                num_radial,
                hidden_channels,
                out_emb_channels,
                out_channels,
                num_output_layers,
                act,
                output_initializer,
            ) for _ in range(num_blocks + 1)
        ])

        self.interaction_blocks = torch.nn.ModuleList([
            InteractionPPBlock(
                hidden_channels,
                int_emb_size,
                basis_emb_size,
                num_spherical,
                num_radial,
                num_before_skip,
                num_after_skip,
                act,
            ) for _ in range(num_blocks)
        ])

        self.reset_parameters()


class DimeNet_Model(torch.nn.Module):

    def __init__(
            self,
            hidden_channels,
            out_channels,
            num_blocks,
            num_bilinear,
            num_spherical,
            num_radial,
            cutoff,
            envelope_exponent,
            num_before_skip,
            num_after_skip,
            num_output_layers,
            one_hot = False
    ):
        super(DimeNet_Model, self).__init__()
        self.encoder = DimeNet(
            hidden_channels=hidden_channels,  # 128
            out_channels=out_channels,  # 1
            num_blocks=num_blocks,  # 4
            num_bilinear=num_bilinear,
            num_spherical=num_spherical,  # 7
            num_radial=num_radial,  # 6
            cutoff=cutoff,  # 6.0
            max_num_neighbors=32,
            envelope_exponent=envelope_exponent,  # 5
            num_before_skip=num_before_skip,  # 1
            num_after_skip=num_after_skip,  # 2
            num_output_layers=num_output_layers,  # 3
            act='swish',
            output_initializer='zeros',
            one_hot=one_hot
        )

        self.ffn_dropout = Dropout(p=0.25)
        self.lin1 = Linear(out_channels, 64)
        self.lin2 = Linear(64, 1)
        self.activate_func = ReLU()
        self.ff_dropout = Dropout(p=0.25)


    def forward(self, batch_data):
        # print(batch_data)
        # if one_hot:
        # atom_index = torch.argmax(batch_data.one_hot_atom, dim = 1)
        graph_embedding = self.encoder(batch_data)

        graph_embedding = self.ffn_dropout(graph_embedding)
        prediction = self.lin2(self.activate_func(self.ff_dropout(self.lin1(graph_embedding))))
        
        return prediction





class DimeNetplusplus_Model(torch.nn.Module):

    def __init__(
            self,
            hidden_channels,
            out_channels,
            num_blocks,
            int_emb_size, 
            basis_emb_size,
            out_emb_channels,
            num_spherical,
            num_radial,
            cutoff,
            max_num_neighbors,
            envelope_exponent,
            num_before_skip,
            num_after_skip,
            num_output_layers,
            one_hot = False
    ):
        super(DimeNetplusplus_Model, self).__init__()
        self.encoder = DimeNetPlusPlus(
            hidden_channels = hidden_channels,
            out_channels = out_channels,
            num_blocks = num_blocks,
            int_emb_size = int_emb_size,
            basis_emb_size = basis_emb_size,
            out_emb_channels = out_emb_channels,
            num_spherical = num_spherical,
            num_radial = num_radial,
            cutoff = cutoff,
            max_num_neighbors = max_num_neighbors,
            envelope_exponent = envelope_exponent,
            num_before_skip = num_before_skip,
            num_after_skip = num_after_skip, 
            num_output_layers = num_output_layers,
            act = 'swish',
            output_initializer = 'zeros',
            one_hot = one_hot
        )


    def forward(self, batch_data):
        # print(batch_data)
        graph_embedding = self.encoder(batch_data)

        return graph_embedding