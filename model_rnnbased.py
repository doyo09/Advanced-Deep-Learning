import pathlib
import sys
import math
from collections import OrderedDict
import traceback

import h5py
import pandas as pd
import numpy as np

import timeit

import torch
import torch.nn as nn
import torch_geometric
import torch_geometric.nn as pyg_nn
from torch import Tensor
from torch.nn import Parameter
import torch.nn.functional as F
from torch.nn import Sequential, Linear, ReLU, Dropout
from torch.nn import BatchNorm1d, LayerNorm, InstanceNorm1d
from torch_sparse import SparseTensor
from torch_scatter import scatter, scatter_softmax
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.nn.norm import MessageNorm

from torch_geometric.nn.inits import reset


import data_utils as utils

from torch_geometric.data import Data, Batch
from torch_scatter import scatter_sum

from collections import defaultdict
from GraphRegion import GraphRegion
from preprocessing import SpatialRegion

from constants import Constants
from activations import Activations

import os

from typing import Optional, List, Union
from torch_geometric.typing import OptPairTensor, Adj, Size, OptTensor

data_dir = pathlib.PosixPath("data/")
dset_name = "porto"

class TrajPositionalEncoding(torch.nn.Module):

    def __init__(self, d_model=50, dropout=0.1, max_len=200):
        super(TrajPositionalEncoding, self).__init__()
        self.dropout = torch.nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)# (1,15,6) batch_first : 
        pe = pe.to(torch.float32)
        self.register_buffer('pe', pe)

    def forward(self, edge_attribute_len):
        """
        edge_attr: batch.edge_attribute_len : (batch, )
        """
#         x = x.to(torch.float32)
#         print(self.pe.size())
        pos_emb = self.pe[torch.cat([torch.arange(edge_l) for edge_l in edge_attribute_len], dim=0)]

        return self.dropout(pos_emb)
    

class MLP(Sequential):
    def __init__(self, channels: List[int], norm: Optional[str] = None,
                 bias: bool = True, dropout: float = 0.):
        m = []
        for i in range(1, len(channels)):
            m.append(Linear(channels[i - 1], channels[i], bias))

            if i < len(channels) - 1:
                if norm and norm == 'batch':
                    m.append(BatchNorm1d(channels[i], affine=True))
                elif norm and norm == 'layer':
                    m.append(LayerNorm(channels[i], elementwise_affine=True))
                elif norm and norm == 'instance':
                    m.append(InstanceNorm1d(channels[i], affine=False))
                elif norm:
                    raise NotImplementedError(
                        f'Normalization layer "{norm}" not supported.')
                m.append(ReLU())
                m.append(Dropout(dropout))

        super(MLP, self).__init__(*m)

def add_self_loops(edge_index, tm_index, verbose=False):
    uniq_node_idx = torch.unique(tm_index)
    self_loops = torch.stack((uniq_node_idx,uniq_node_idx), dim=0) # (2, #uniq_node_idx)
    new_edge_index = torch.cat((edge_index, self_loops), dim=1)
    if verbose:
        print("new_edge_index size : ", new_edge_index.size())
    return new_edge_index

class GENConv(MessagePassing):
    r"""The GENeralized Graph Convolution (GENConv) from the `"DeeperGCN: All
    You Need to Train Deeper GCNs" <https://arxiv.org/abs/2006.07739>`_ paper.
    Supports SoftMax & PowerMean aggregation. The message construction is:

    .. math::
        \mathbf{x}_i^{\prime} = \mathrm{MLP} \left( \mathbf{x}_i +
        \mathrm{AGG} \left( \left\{
        \mathrm{ReLU} \left( \mathbf{x}_j + \mathbf{e_{ji}} \right) +\epsilon
        : j \in \mathcal{N}(i) \right\} \right)
        \right)

    .. note::

        For an example of using :obj:`GENConv`, see
        `examples/ogbn_proteins_deepgcn.py
        <https://github.com/rusty1s/pytorch_geometric/blob/master/examples/
        ogbn_proteins_deepgcn.py>`_.

    Args:
        in_channels (int): Size of each input sample.
        out_channels (int): Size of each output sample.
        aggr (str, optional): The aggregation scheme to use (:obj:`"softmax"`,
            :obj:`"softmax_sg"`, :obj:`"power"`, :obj:`"add"`, :obj:`"mean"`,
            :obj:`max`). (default: :obj:`"softmax"`)
        t (float, optional): Initial inverse temperature for softmax
            aggregation. (default: :obj:`1.0`)
        learn_t (bool, optional): If set to :obj:`True`, will learn the value
            :obj:`t` for softmax aggregation dynamically.
            (default: :obj:`False`)
        p (float, optional): Initial power for power mean aggregation.
            (default: :obj:`1.0`)
        learn_p (bool, optional): If set to :obj:`True`, will learn the value
            :obj:`p` for power mean aggregation dynamically.
            (default: :obj:`False`)
        msg_norm (bool, optional): If set to :obj:`True`, will use message
            normalization. (default: :obj:`False`)
        learn_msg_scale (bool, optional): If set to :obj:`True`, will learn the
            scaling factor of message normalization. (default: :obj:`False`)
        norm (str, optional): Norm layer of MLP layers (:obj:`"batch"`,
            :obj:`"layer"`, :obj:`"instance"`) (default: :obj:`batch`)
        num_layers (int, optional): The number of MLP layers.
            (default: :obj:`2`)
        eps (float, optional): The epsilon value of the message construction
            function. (default: :obj:`1e-7`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.GenMessagePassing`.
    """
    def __init__(self, in_channels: int, out_channels: int, edge_pos_emb=False,
                 aggr: str = 'softmax', t: float = 1.0, learn_t: bool = False,
                 p: float = 1.0, learn_p: bool = False, msg_norm: bool = False,
                 learn_msg_scale: bool = False, norm: str = 'batch',
                 num_layers: int = 2, eps: float = 1e-7, **kwargs):

        kwargs.setdefault('aggr', None)
        super(GENConv, self).__init__(**kwargs)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.aggr = aggr
        self.eps = eps

        assert aggr in ['softmax', 'softmax_sg', 'power']

        channels = [in_channels]
        for i in range(num_layers - 1):
            channels.append(in_channels * 2)
        channels.append(out_channels)
        self.mlp = MLP(channels, norm=norm)

        self.msg_norm = MessageNorm(learn_msg_scale) if msg_norm else None

        self.initial_t = t
        self.initial_p = p

        if learn_t and aggr == 'softmax':
            self.t = Parameter(torch.Tensor([t]), requires_grad=True)
        else:
            self.t = t

        if learn_p:
            self.p = Parameter(torch.Tensor([p]), requires_grad=True)
        else:
            self.p = p
            
        if edge_pos_emb:
            self.edge_enc = TrajPositionalEncoding(d_model=out_channels,
                                                  max_len=110)
        else :
            self.edge_enc = None
            
    def reset_parameters(self):
        reset(self.mlp)
        if self.msg_norm is not None:
            self.msg_norm.reset_parameters()
        if self.t and isinstance(self.t, Tensor):
            self.t.data.fill_(self.initial_t)
        if self.p and isinstance(self.p, Tensor):
            self.p.data.fill_(self.initial_p)


    def forward(self, x: Union[Tensor, OptPairTensor], edge_index: Adj,
                edge_attr: OptTensor = None, edge_attr_len:OptTensor=None,
                size: Size = None, tm_index=None, duplicates_idx=None) -> Tensor:
        """"""
        
        if isinstance(x, Tensor):
            x: OptPairTensor = (x, x)
        
        # add_self_loops
        if tm_index is not None:
            edge_index = add_self_loops(edge_index, tm_index, verbose=False)
            
        # propagate_type: (x: OptPairTensor, edge_attr: OptTensor)
        out = self.propagate(edge_index, x=x, edge_attr=edge_attr,
                             edge_attr_len=edge_attr_len, size=size,
                             duplicates_idx=duplicates_idx)
        
        if self.msg_norm is not None:
            out = self.msg_norm(x[0], out)

        x_r = x[1]
        if x_r is not None:
            out += x_r
        
        
        return self.mlp(out)


    def message(self, x_j: Tensor, edge_attr: OptTensor, edge_attr_len: OptTensor, 
                duplicates_idx: OptTensor) -> Tensor:
#         print('in message')
#         print('x_j.size(), edge_attr.size()', x_j.size(), edge_attr.size())
        just_made_dup = False
        
        if duplicates_idx is None: # 1st layer, so make duplicates_idx
            just_made_dup = True
            edge_indice, cnt_ = torch.unique(edge_attr, return_counts=True,)
            duplicates_idx = [[idx]*(c-1) for idx, c in zip(edge_indice[cnt_!=1], cnt_[cnt_!=1])]
            duplicates_idx = [_ for li in duplicates_idx for _ in li]
            
        # duplicates_idx is not None
        x_j = torch.cat((x_j,x_j[duplicates_idx,]), dim=0) # ((E+num_duplicates),H)
#         print("duplicates_idx: ", duplicates_idx)
#         print("x_j: ", x_j)
        
        if just_made_dup:
            for i,idx in enumerate(duplicates_idx):
                edge_attr[torch.arange(edge_attr.size(0))[edge_attr == idx][-1]] = x_j.size(0)-len(duplicates_idx)+i
                
#         print('x_j.size(), edge_attr.size()', x_j.size(), edge_attr.size())
        
        if self.edge_enc:
#             print("Adding edge_enc ... ")
            edge_pos_emb = self.edge_enc(edge_attr_len)
#             print(edge_pos_emb.size())
            x_j[edge_attr] += edge_pos_emb.squeeze()
            
            
#         print("x_j size", x_j.size())
        self.duplicates_idx = duplicates_idx
            
        return F.relu(x_j) + self.eps, duplicates_idx

    def aggregate(self, inputs, index: Tensor,
                  dim_size: Optional[int] = None) -> Tensor:
        
        inputs, duplicates_idx = inputs
        index = torch.cat((index,index[duplicates_idx,]), dim=0)
#         print('duplicates_idx: ',duplicates_idx)
        
        if self.aggr == 'softmax':
            out = scatter_softmax(inputs * self.t, index, dim=self.node_dim)
            return scatter(inputs * out, index, dim=self.node_dim,
                           dim_size=dim_size, reduce='sum')

        elif self.aggr == 'softmax_sg':
            out = scatter_softmax(inputs * self.t, index,
                                  dim=self.node_dim).detach()
            return scatter(inputs * out, index, dim=self.node_dim,
                           dim_size=dim_size, reduce='sum')

        else:
            min_value, max_value = 1e-7, 1e1
            torch.clamp_(inputs, min_value, max_value)
            out = scatter(torch.pow(inputs, self.p), index, dim=self.node_dim,
                          dim_size=dim_size, reduce='mean')
            torch.clamp_(out, min_value, max_value)
            return torch.pow(out, 1 / self.p)

    def __repr__(self):
        return '{}({}, {}, aggr={})'.format(self.__class__.__name__,
                                            self.in_channels,
                                            self.out_channels, self.aggr)
    
spatialregion_fname = "preprocessed_entire_porto.pkl"
adj_fname = "preprocessed_entire_porto_sparseadjmatrix.pt"

graphregion = GraphRegion(dataset_name="porto",
                          minlon=-8.735152, minlat=40.953673,
                          maxlon=-8.156309, maxlat=41.307945,
                          xstep=100, ystep=100,)
graphregion.load_graphregion_info(data_dir,
                                  spatialregion_fname,
                                  adj_fname
                                 )

def vocab2offset_normalized(vocab):
    cell_id = graphregion.vocab2hotcell[vocab]
    yoffset = cell_id // graphregion.numx
    xoffset = cell_id % graphregion.numx
    
    #normalize
    yoffset = yoffset/graphregion.numy
    xoffset = xoffset/graphregion.numx
    return (xoffset, yoffset)

graphregion.vocab2offset_normalized = dict()

for i in range(graphregion.vocab_start):
    graphregion.vocab2offset_normalized[i] = (0.,0.)
for vocab in range(graphregion.vocab_start,graphregion.vocab_size):
    graphregion.vocab2offset_normalized[vocab] = vocab2offset_normalized(vocab)

class TransformerModel(nn.Module):

    def __init__(self, ninp, nhead, nhid, nlayers, dropout=0.5):
        """
        @param ninp : input hidden size
        @param nhid : output hidden size for following FFNs
        @param nhead : nom of multiheads
        """
        
        super(TransformerModel, self).__init__()
        from torch.nn import TransformerEncoder, TransformerEncoderLayer
        self.model_type = 'Transformer'
        self.src_mask = None
        self.traj_encoder = TrajPositionalEncoding(d_model=nhid, max_len=120).to(torch.float32)
        encoder_layers = TransformerEncoderLayer(ninp, nhead, nhid, dropout,).to(torch.float32)
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers, nn.LayerNorm(nhid,)).to(torch.float32)
        self.ninp = ninp

    def forward(self, src, traj_len, src_key_padding_mask=None):
        """
        @param src : (N, T, D) = (1,14,6)
        @param traj_len : lengths of trajs e.g. batch.traj_len
        """
        src = src.transpose(0,1).contiguous() # (T, N, D) : (14,1,6)
        src = src * math.sqrt(self.ninp)
        
        # add trajectory positional encoding
        tm_emb = self.traj_encoder(traj_len)
        tm_emb = torch.nn.utils.rnn.pad_sequence(torch.split(tm_emb.squeeze(), traj_len.tolist()), 
                                padding_value=0,
                                batch_first=True)
        
        src[torch.arange(traj_len.max()).unsqueeze(-1),torch.arange(src.size(1)),:] += tm_emb.transpose(0,1).contiguous()

        src = src.to(torch.float32)
        # src_key_padding_mask
        output = self.transformer_encoder(src,
                                          src_key_padding_mask = src_key_padding_mask) 
        output = output.transpose(0,1).contiguous() 
        return output.to(torch.float32)
    
class TrajecotryEncoderLayer(nn.Module):
    def __init__(self, config, firstlayer=False):
        super(TrajecotryEncoderLayer, self).__init__()
        self.gcn = GENConv(in_channels=config.hidden_size, out_channels=config.hidden_size, 
                           edge_pos_emb=firstlayer, norm='layer')
        self.tm = TransformerModel(ninp=config.hidden_size, nhead=config.num_attention_heads,
                                   nhid=config.hidden_size, nlayers=1,
                                   dropout=config.hidden_dropout_prob)
        
    def forward(self, x, edge_index, tm_index, tm_len, traj_len,
                edge_attribute, edge_attribute_len,src_key_padding_mask=None, duplicates_idx=None,
                pre_out=None, pre_out_tm=None, residual=False):
#         print("outside gcn, edge_index.size(): ", edge_index.size())
        out = self.gcn(x, 
                       edge_index,
                       edge_attr= edge_attribute,
                       edge_attr_len = edge_attribute_len,
                       tm_index = tm_index,
                       duplicates_idx=duplicates_idx)
        if (residual) and (pre_out is not None):
            out = out+pre_out
        
#         out.size()
        
        tm_input = torch.nn.utils.rnn.pad_sequence(torch.split(out[tm_index], 
                                                               tm_len.tolist()),
                                                   batch_first=True, 
                                                   padding_value=0,) # (N,D) to (N,T,D)
#         print('tm_input.size(): ', tm_input.size())
        
        # src_key_padding_mask is added
        out_tm = self.tm(tm_input, traj_len, src_key_padding_mask=src_key_padding_mask)
        
        if residual and (pre_out_tm is not None):
            out_tm = out_tm+pre_out_tm


        out[tm_index] = torch.cat([out_tm[i,torch.arange(length),:] for i,length in enumerate(tm_len.tolist())],
                                        dim=0)

    
        return out, out_tm, self.gcn.duplicates_idx, 

    
class TrajectoryEncoder(nn.Module):
    def __init__(self, config,):
        super(TrajectoryEncoder, self).__init__()
        
        self.spatial_linear = nn.Linear(in_features=config.spatial_input_size,
                                        out_features=config.hidden_size,)
        self.cell_emb = nn.Embedding(num_embeddings=graphregion.vocab_size, 
                                embedding_dim=config.hidden_size, 
                                padding_idx=config.pad_token_id,)

        
        self.layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob,) 
        # firstlayer=True for the first layer
#         self.trajecotry_encoder_layer = TrajecotryEncoderLayer(config, firstlayer=True)
        self.trajecotry_encoder_layers = nn.ModuleList([TrajecotryEncoderLayer(config,
                                                                               firstlayer=False,
                                                                              ) for _ in range(config.num_hidden_layers-1)])
        self.trajecotry_encoder_layers.insert(0, TrajecotryEncoderLayer(config, firstlayer=True))
        
        self.rnn = nn.LSTM(input_size=config.hidden_size,
                 hidden_size=config.hidden_size,
                 num_layers=1, batch_first=True).to(config.device)
        
        self.config = config
        
    
    def forward(self, batch, duplicates_idx=None, pre_out=None, pre_out_tm=None, residual=True):
        """
        @param batch : Data object
        """
        x = batch.x.to(self.config.device)
        edge_index = batch.edge_index.to(self.config.device)
        edge_attribute = batch.edge_attribute.to(self.config.device)
        edge_attribute_len = batch.edge_attribute_len.to(self.config.device)
        tm_index = batch.tm_index.to(self.config.device)
        tm_len = batch.tm_len.to(self.config.device)
        traj_len = batch.traj_len.to(self.config.device)
        
        # get_pad_mask : torch.bool
        src_key_padding_mask = self.get_pad_mask(x[tm_index], tm_len,).squeeze().detach().clone()
        self.src_key_padding_mask = src_key_padding_mask
#         print('src_key_padding_mask', src_key_padding_mask.size(), src_key_padding_mask)
        
        spatial_emb = Activations[self.config.activation](self.spatial_linear(torch.tensor(list(graphregion.vocab2offset_normalized.values()),
                                                            dtype=torch.float32).to(self.config.device)))

        c_emb = self.cell_emb(x[torch.unique(tm_index)]).squeeze()
        s_emb = spatial_emb[x[torch.unique(tm_index)]].squeeze()
        
        x = torch.zeros((x.size(0),self.config.hidden_size),
                        dtype=torch.float32).to(self.config.device)
        x[torch.unique(tm_index)] = self.dropout(self.layernorm(c_emb+s_emb))
        
        
        for i, layer_module in enumerate(self.trajecotry_encoder_layers):
            """def forward(self, x, edge_index, tm_index, tm_len, traj_len,
                edge_attribute, edge_attribute_len, duplicates_idx=None,
                pre_out=None, pre_out_tm=None, residual=False):"""
            pre_out, pre_out_tm, duplicates_idx = layer_module(x, edge_index, tm_index,
                                                               tm_len, traj_len,
                                                               edge_attribute, 
                                                               edge_attribute_len,
                                                               src_key_padding_mask=self.src_key_padding_mask, 
                                                               duplicates_idx=duplicates_idx,     
                                                               pre_out=pre_out,
                                                               pre_out_tm=pre_out_tm, 
                                                               residual=residual)
        # N, traj_len, D
        traj_emb = torch.nn.utils.rnn.pad_sequence([sample[:l] for sample, l in zip(pre_out_tm, traj_len)],
                                                   batch_first=True, 
                                                   padding_value=0) # N, left.traj_len, D
        traj_emb, _ = self.rnn(traj_emb) # N, left.traj_len, D
        
        # put it back to gnn output
        splited_tm = torch.split((tm_index), tm_len.tolist())
        splited_tm = [sample_tm[:traj_l] for sample_tm, traj_l in zip(splited_tm, traj_len)]
        tognn_idx = torch.cat(splited_tm) # sum of traj_len
        pre_out[tognn_idx] = torch.cat([traj_emb[i,torch.arange(traj_l),:] for i,traj_l 
                                        in enumerate(traj_len.tolist())], dim=0) 
        # (sum of traj_len, D)

        
#         traj_emb = traj_emb[torch.arange(traj_emb.size(0)), traj_len-1,:] # N, D
        
        return pre_out, pre_out_tm, traj_emb # traj_emb : N, left.traj_len, D
    
    def get_pad_mask(self, vocab_idx, tm_len, i_pad=0):
        """
        @param vocab_idx : batch.x[batch.tm_idx];
        otherwise, padding idx 0 and node idx 0 will be messed up.
        """
        pad_mask = torch.nn.utils.rnn.pad_sequence(torch.split(vocab_idx, 
                                                               tm_len.tolist()),
                                                   batch_first=True, 
                                                   padding_value=i_pad,).eq(i_pad)
        return pad_mask
    
class DestinationProjHead(nn.Module):
    
    def __init__(self, config, weights = None):
        super(DestinationProjHead, self).__init__()
        self.n_negs = config.n_negatives
        self.vocab_size = config.vocab_size
        self.config = config
        
        self.attn_head = nn.Linear(config.hidden_size, config.hidden_size)
        self.proj_head = nn.Linear(config.hidden_size, config.hidden_size)
        self.last_head = nn.Linear(config.hidden_size, config.hidden_size)
        
        self.layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        
    def forward(self, out_tm, src_key_padding_mask=None):
        """
        @param out_tm : (N,T,D)
        @param src_key_padding_mask : (N, T); fill some val if mask is True
        """
        bs = out_tm.size(0)
        w = self.attn_head(out_tm)
        # here is where attn mask is needed
        if src_key_padding_mask is not None : 
            sum_mask = torch.einsum('bp, bq->bpq', (~src_key_padding_mask), (~src_key_padding_mask))
            w = torch.bmm(w, w.transpose(2,1).contiguous()).masked_fill(~sum_mask, float(0.)) # n,t,d -> n,t,t & sum_mask
            w = torch.sum(w, axis=1, keepdim=True).squeeze()/(2*(self.config.hidden_size**.5)) # n,t
            w = nn.Softmax(dim=1)(w.masked_fill(src_key_padding_mask, -float('inf')))
            self.w_masked = w
        else :             
            w = nn.Softmax(dim=1)(torch.sum(torch.bmm(w, w.transpose(2,1).contiguous()), 
                                       axis=1, keepdim=True).squeeze()/(2*(self.config.hidden_size**.5))) # (N,T)
        
        out_tm = self.proj_head(out_tm) # (N,T,D)
        out_tm = self.layernorm(torch.bmm(w.unsqueeze(1), out_tm).squeeze()) # (N,D)
        out_tm = Activations[self.config.activation](out_tm)
        logits = self.last_head(out_tm) #(N, hidden_size)

        return logits

class AugMask(nn.Module):
    def __init__(self, config=None,):
        super(AugMask, self).__init__()
        self.config=config
    def forward(self, aug_input, aug_input_mask):
        """
        both are n,t,d
        """
        aug_masked = aug_input.masked_fill(aug_input_mask, float(0))
        return aug_masked

class AugProjHead(nn.Module):
    
    def __init__(self, config,):
        super(AugProjHead, self).__init__()
        self.config = config
        self.mask = AugMask()
        self.proj_head1 = nn.Sequential(OrderedDict([
            ('linear1', nn.Linear(config.hidden_size, config.hidden_size)),
            (self.config.activation, Activations[self.config.activation]),
        ]))
        self.proj_head2 = nn.Sequential(OrderedDict([
            ('linear2', nn.Linear(config.hidden_size, config.hidden_size)),
            (self.config.activation, Activations[self.config.activation]),
        ]))
        self.proj_head3 = nn.Sequential(OrderedDict([
            ('linear3', nn.Linear(config.hidden_size, config.hidden_size)),
            (self.config.activation, Activations[self.config.activation]),
        ]))

    def forward(self, l_aug_input, r_aug_input):
        """
        @param l_aug_input or r_aug_input : (N,T(edge_attr_num),D)
        """
        l_aug_input_mask, r_aug_input_mask = l_aug_input.eq(0).detach().clone(), r_aug_input.eq(0).detach().clone()
        
        l_aug_out = self.proj_head1(l_aug_input) # (N,T,D)
        l_aug_out = self.mask(l_aug_out,l_aug_input_mask)
        l_aug_out = self.proj_head2(l_aug_input) # (N,T,D)
        l_aug_out = self.mask(l_aug_out,l_aug_input_mask)
        l_aug_out = self.proj_head3(l_aug_input) # (N,T,D)
        l_aug_out = self.mask(l_aug_out,l_aug_input_mask)
        
        r_aug_out = self.proj_head1(r_aug_input) # (N,T,D)
        r_aug_out = self.mask(r_aug_out,l_aug_input_mask)
        r_aug_out = self.proj_head2(r_aug_out) # (N,T,D)
        r_aug_out = self.mask(r_aug_out,l_aug_input_mask)
        r_aug_out = self.proj_head3(r_aug_out) # (N,T,D)
        r_aug_out = self.mask(r_aug_out,l_aug_input_mask)
        
        # (l_aug_out \odot r_aug_out) sum along axis D
        logits = torch.sum(l_aug_out * r_aug_out, dim=-1) # N, T
        
        return logits
    
class MapembProjHead(nn.Module):
    
    def __init__(self, config,):
        super(MapembProjHead, self).__init__()
        self.config = config
#         self.attn_head = nn.Linear(config.hidden_size, config.hidden_size)
        self.layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.proj_head = nn.Sequential(OrderedDict([
            ('linear1', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation]),
            ('linear2', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation]),
        ]))
        
    def forward(self, anc_mean, pos_mean, neg_mean, 
               ):
        """
        @param *_perm_input : (N,D)
        """
        
        anc_mean = self.proj_head(anc_mean,) # N,D
        pos_mean = self.proj_head(pos_mean,) # N,D
        neg_mean = self.proj_head(neg_mean,) # N,D

        return anc_mean, pos_mean, neg_mean
    
class MaskedProjHead(nn.Module):
    
    def __init__(self, config, ):
        super(MaskedProjHead, self).__init__()
        self.n_negs = config.n_negatives
        self.config = config
        self.layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        
        self.proj_head = nn.Sequential(OrderedDict([
            ('linear1', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation]),
            ('linear2', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation]),
            ('linear3', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation])
        ]))
        
    def forward(self, out_tm,):
        """
        @param out_tm : (N,D)
        """
        logits = self.proj_head(out_tm) # (N,D)
        return logits # (N,D)
    
class PermProjHead(nn.Module):
    
    def __init__(self, config,):
        super(PermProjHead, self).__init__()
        self.config = config
#         self.attn_head = nn.Linear(config.hidden_size, config.hidden_size)
        self.layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.proj_head = nn.Sequential(OrderedDict([
            ('linear1', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation]),
            ('linear2', nn.Linear(config.hidden_size, config.hidden_size)),
            ('layernorm', self.layernorm),
            (self.config.activation, Activations[self.config.activation]),
        ]))
        
        self.last_head = nn.Linear(2*config.hidden_size, config.perm_class_num)
        
    def forward(self, anc_perm_input, pos_perm_input, neg_perm_input, 
                anc_src_key_padding_mask=None,
                pos_src_key_padding_mask=None,
                neg_src_key_padding_mask=None
               ):
        """
        @param *_perm_input : (N,D)
        """
        
        anc_perm_input = self.proj_head(anc_perm_input,) # N,D
        pos_perm_input = self.proj_head(pos_perm_input,) # N,D
        neg_perm_input = self.proj_head(neg_perm_input,) # N,D
        
        similarity = self.last_head(torch.cat((pos_perm_input, neg_perm_input), dim=1))
        return similarity
    
   
def weights_init_classifier(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
#         nn.init.normal_(m.weight.data, std=0.02)
        torch.nn.init.xavier_uniform(m.weight)
        nn.init.constant_(m.bias.data, 0.0)
        
#     elif classname.find('LSTM') != -1:
#         for value in m.state_dict():

#                 #format values
#             param = m.state_dict()[value]
#             if 'weight_ih' in value:
#                 D = param.size(1)
#                 #print(value,param.shape,'Orthogonal')
#                 weight_hh_data_ii = torch.eye(D,D)#H_Wii
#                 weight_hh_data_if = torch.eye(D,D)#H_Wif
#                 weight_hh_data_ic = torch.eye(D,D)#H_Wic
#                 weight_hh_data_io = torch.eye(D,D)#H_Wio
#                 weight_hh_data = torch.stack([weight_hh_data_ii,weight_hh_data_if,weight_hh_data_ic,weight_hh_data_io], dim=0)
#                 weight_hh_data = weight_hh_data.view(D*4,D)
#                 #print(value,param.shape,weight_hh_data.shape,self.number_of_layers,self.hidden_units,'Identity')
#                 m.state_dict()[value].data.copy_(weight_hh_data)#hidden TO hidden IDENTITY.state_dict()[value].data.copy_(weight_hh_data)#hidden TO hidden IDENTITY
#             elif 'weight_hh' in value:
#                 D = param.size(1)
#                 #INITIALIZE SEPERATELY EVERY MATRIX TO BE THE IDENTITY AND THE STACK THEM                        
#                 weight_hh_data_ii = torch.eye(D,D)#H_Wii
#                 weight_hh_data_if = torch.eye(D,D)#H_Wif
#                 weight_hh_data_ic = torch.eye(D,D)#H_Wic
#                 weight_hh_data_io = torch.eye(D,D)#H_Wio
#                 weight_hh_data = torch.stack([weight_hh_data_ii,weight_hh_data_if,weight_hh_data_ic,weight_hh_data_io], dim=0)
#                 weight_hh_data = weight_hh_data.view(D*4,D)
#                 #print(value,param.shape,weight_hh_data.shape,self.number_of_layers,self.hidden_units,'Identity')
#                 m.state_dict()[value].data.copy_(weight_hh_data)#hidden TO hidden IDENTITY.state_dict()[value].data.copy_(weight_hh_data)#hidden TO hidden IDENTITY
#             elif 'bias' in value:
#                     #print(value,param.shape,'Zeros')
#                 torch.nn.init.constant_(m.state_dict()[value], val=0)
#                 m.state_dict()[value].data[D:D*2].fill_(1)#set the forget gate | (b_ii|b_if|b_ig|b_io)

    
        

        
def compute_destination_loss(batch, traj_encoder,
                             dest_proj, graphregion, config,) :
    
    # adjacent answers K=20
    vocab_y = batch.x[batch.y].detach().clone() # vocab_index from batch.y(batch-based)
    cells_y = [graphregion.vocab2hotcell[v.item()] for v in vocab_y] # vocab to cell_id
    near_cells_y, dist = graphregion.knearest_hotcells(cells_y, config.k_near_vocabs) # kNN of cell_ids
    # Knear_cell_ids -> vocabs
    near_vocabs = torch.tensor([[graphregion.hotcell2vocab[cell] for cell in near_cells] for near_cells in near_cells_y]).to(config.device, torch.long)
    near_vocabs_emb = traj_encoder.cell_emb(near_vocabs).detach().clone() # (batch_size, K, emb_size)
    # The closer, the bigger penalty
    spatial_weight = nn.Softmax()(-torch.tensor(dist/config.temp).to(config.device),) #(batch_size, K)

    # TrajEncoder or t2vec :  traj_emb (N, traj_len, D)
    batch_out, out_tm, traj_emb = traj_encoder(batch)

    # negeative samples : (T, n_negs=500, emb)
    weights = torch.ones(config.vocab_size)/config.vocab_size
    negs = torch.multinomial(weights, 
                                 out_tm.size(0)*config.k_near_vocabs * config.n_negatives, 
                                 replacement=True).view(out_tm.size(0), -1).to(config.device)

    # Destination Projection
    traj_mask = torch.nn.utils.rnn.pad_sequence([torch.ones(traj_l) for traj_l in batch.traj_len],
                                                   batch_first=True, 
                                                   padding_value=0,).eq(0) # N,traj_len
    traj_mask = traj_mask.to(config.device)
    
    h_t = dest_proj(traj_emb, 
                    src_key_padding_mask=traj_mask) # (bs, hidden_size)
    # or traj_encoder.src_key_padding_mask
    
    # compute loss
    w_uh_t = torch.bmm(near_vocabs_emb,h_t.unsqueeze(-1),) # (bs, K, 1)
    neg_term = torch.bmm(traj_encoder.cell_emb(negs).detach().clone(), h_t.unsqueeze(-1)) # (bs, K *n_negs, 1)
    neg_term = torch.exp(neg_term.view(out_tm.size(0), config.k_near_vocabs,
                             config.n_negatives,-1).contiguous()) # (bs, K, n_negs, 1)
    neg_term = torch.sum(neg_term, dim=2) # (bs, K, 1)

    loss_destination = torch.sum(-spatial_weight * (w_uh_t - neg_term).squeeze()) # (bs, K) -> ()
    loss_destination /= (h_t.size(0)*config.k_near_vocabs)
    return loss_destination, out_tm, h_t, w_uh_t, negs, neg_term

def compute_aug_loss(left, right, traj_encoder, aug_proj, mapemb_proj,
                             graphregion, config, criterion_ce, is_mapemb=False):
    
    # make node_ids for augmentation : (N,T_hat,D)    
    l_edge_ids = left.edge_index[:,left.edge_attribute]
    r_edge_ids = right.edge_index[:,right.edge_attribute]

    l_edgeids_foraug = torch.split(l_edge_ids, left.edge_attribute_len.tolist(), dim=1)
    r_edgeids_foraug = torch.split(r_edge_ids, right.edge_attribute_len.tolist(), dim=1)

    l_edgeids_foraug = torch.cat([torch.cat((l_edgeids_foraug[0], 
                          l_edgeids_foraug[1,-1].unsqueeze(-1))) for l_edgeids_foraug in l_edgeids_foraug],
             dim = 0).detach().clone()
    r_edgeids_foraug = torch.cat([torch.cat((r_edgeids_foraug[0], 
                          r_edgeids_foraug[1,-1].unsqueeze(-1))) for r_edgeids_foraug in r_edgeids_foraug],
             dim = 0).detach().clone()
    
    # left.x, right.x
    left_out, _, _ = traj_encoder(left)
    right_out, _, _ = traj_encoder(right)
    
    if is_mapemb: # compute mapembloss: min_uniquemovelen > 50
        loss_mapemb = compute_mapemb_loss(left_out.clone(),
                                          mapemb_proj,
                                          left.edge_index,
                                          left.edge_attribute,
                                          left.edge_attribute_len,
                                          config)
    else :
        loss_mapemb = None

    l_aug_input = torch.nn.utils.rnn.pad_sequence(torch.split(left_out[l_edgeids_foraug], 
                                                (left.edge_attribute_len+1).tolist()
                                               ),
                                    batch_first=True, 
                                    padding_value=0) # N,T(edge_attr_num),D
    r_aug_input = torch.nn.utils.rnn.pad_sequence(torch.split(right_out[r_edgeids_foraug], 
                                                (right.edge_attribute_len+1).tolist()
                                               ),
                                    batch_first=True, 
                                    padding_value=0) # N,T(edge_attr_num),D
    # to aug_projhead(l_aug_input, r_aug_input)
    logits = aug_proj(l_aug_input, r_aug_input) # (N, T(edge_attr_num)+1)
    loss_augmentation = criterion_ce(logits, left.y.to(config.device)) # cross_entropy here
    
    # compute_mapemb_loss(batch, out_gnn.clone())
    return loss_augmentation, loss_mapemb

def compute_mapemb_loss(out_gnn, mapemb_proj, edge_index, edge_attribute,edge_attribute_len, config):
    """
    @param batch : left
    """
    # edge_attribute to edge_idx
    map_pos_neg_edgeidx = [torch.stack((edgeattr[0],edgeattr[4], edgeattr[-1]),
                              dim=0) for edgeattr in torch.split(edge_attribute, edge_attribute_len.tolist())]
    map_pos_neg_edgeidx = torch.stack(map_pos_neg_edgeidx,dim=0)
    
    # edge_idx to node_index
    map_pos_neg_nodeidx = edge_index[0, map_pos_neg_edgeidx]
    conn_nodes_idx = [torch.unique(edge_index[:,torch.any(edge_index == node_idx, dim=0)].view(-1), sorted=False) for node_idx in map_pos_neg_nodeidx.view(-1)] #list of tensors
    # node_index to connected_nodes_indice
    conn_nodes_idx = [node_indice[torch.randperm(node_indice.nelement())][:10] for node_indice in conn_nodes_idx] # get maximum 10 indice
    # connected_nodes_indice prototype
    apn = [torch.mean(out_gnn[node_indice[:10]], dim=0) for node_indice in conn_nodes_idx]
    apn = torch.stack(apn, dim=0) # (3*bs, D)
    # Mapemb Proj
    # anc, pos, neg = mapemb_proj(apn[::3], apn[1::3], apn[2::3])
    anc, pos, neg =  mapemb_proj(apn[::3], apn[1::3], apn[2::3])
    # tripletloss
    pos_dist = torch.sum(torch.square(anc-pos),dim=1) # anchor-pos: (bs,)
    neg_dist = torch.sum(torch.square(anc-neg),dim=1) # anchor-neg: (bs,)
    loss_mapemb = torch.sum(torch.max(pos_dist-neg_dist+0.5, torch.tensor(0., dtype=torch.float32).to(config.device))) / pos_dist.size(0)
    
    return loss_mapemb

def compute_mask_loss(batch, traj_encoder, mask_proj, 
                                          graphregion, config,):
    batch_queries = torch.arange(batch.x.size(0))[(batch.x==3).squeeze()] # (bs,)
    assert batch_queries.size(0) == batch.y.size(0)
    # adjacent answers K=20
    cells_y = [graphregion.vocab2hotcell[v.item()] for v in batch.y] # vocab to cell_id
    near_cells_y, dist = graphregion.knearest_hotcells(cells_y, config.k_near_vocabs) # kNN of cell_ids
    # Knear_cell_ids -> vocabs
    near_vocabs = torch.tensor([[graphregion.hotcell2vocab[cell] for cell in near_cells] for near_cells in near_cells_y]).to(config.device)
    near_vocabs_emb = traj_encoder.cell_emb(near_vocabs).detach().clone() # (batch_size, K, emb_size)
    # The closer, the bigger penalty
    spatial_weight = nn.Softmax()(-torch.tensor(dist/config.temp).to(config.device),) #(batch_size, K)

    # TrajEncoder
    batch_out, _, _ = traj_encoder(batch)
    batch_queries = batch_out[batch_queries] # (bs, emb)

    # negeative samples : (T, n_negs=500, emb)
    weights = torch.ones(config.vocab_size)/config.vocab_size
    negs = torch.multinomial(weights, 
                                 batch_queries.size(0)*config.k_near_vocabs * config.n_negatives, 
                                 replacement=True).view(batch_queries.size(0), -1).to(config.device)

    # Mask Projection
    h_t = mask_proj(batch_queries,) # (bs, hidden_size)
    # compute loss
    w_uh_t = torch.bmm(near_vocabs_emb, h_t.unsqueeze(-1),) # (bs, K, 1)
    _neg_term = torch.bmm(traj_encoder.cell_emb(negs).detach().clone(), h_t.unsqueeze(-1)) # (bs, K *n_negs, 1)
    neg_term = torch.exp(_neg_term.view(h_t.size(0), config.k_near_vocabs,
                             config.n_negatives, -1).contiguous()) # (bs, K, n_negs, 1)

    neg_term = torch.sum(neg_term, dim=2) # (bs, K, 1)
    loss_mask = torch.sum(-spatial_weight * (w_uh_t - neg_term).squeeze()) # (bs, K) -> ()
    loss_mask /= (h_t.size(0)*config.k_near_vocabs) # bs*k
    return loss_mask, batch_queries,h_t, w_uh_t, _neg_term, neg_term,  

def compute_perm_loss(anchor, pos, neg,
                      traj_encoder, perm_proj, 
                      graphregion, config, criterion_ce):
    
    # make node_ids for perm : (N,T_hat,D)
    _, _, anc_trajemb = traj_encoder(anchor)
    _, _, pos_trajemb = traj_encoder(pos)
    _, _, neg_trajemb = traj_encoder(neg)
    anc_trajemb = anc_trajemb[torch.arange(anc_trajemb.size(0)), anchor.traj_len-1,:]
    pos_trajemb = pos_trajemb[torch.arange(pos_trajemb.size(0)), pos.traj_len-1,:]
    neg_trajemb = neg_trajemb[torch.arange(neg_trajemb.size(0)), neg.traj_len-1,:]
    # make input for perm : (N, left.traj_len, D)    
#     anc_perm_input = torch.nn.utils.rnn.pad_sequence([sample[:l] for sample, l in zip(anc_out_tm, anchor.traj_len)],
#                                                    batch_first=True, 
#                                                    padding_value=0) # N, left.traj_len, D
#     anc_src_key_padding_mask = torch.nn.utils.rnn.pad_sequence([torch.ones(l) for l in anchor.traj_len],
#                                                              batch_first=True,
#                                                              padding_value=0).eq(0)
#     anc_src_key_padding_mask = anc_src_key_padding_mask.to(config.device)

#     pos_perm_input = torch.nn.utils.rnn.pad_sequence([sample[:l] for sample, l in zip(pos_out_tm, pos.traj_len)],
#                                                    batch_first=True, 
#                                                    padding_value=0) # N, right.traj_len, D
#     pos_src_key_padding_mask = torch.nn.utils.rnn.pad_sequence([torch.ones(l) for l in pos.traj_len],
#                                                              batch_first=True,
#                                                              padding_value=0).eq(0)
#     pos_src_key_padding_mask = pos_src_key_padding_mask.to(config.device)

#     neg_perm_input = torch.nn.utils.rnn.pad_sequence([sample[:l] for sample, l in zip(neg_out_tm, neg.traj_len)],
#                                                    batch_first=True, 
#                                                    padding_value=0) # N, right.traj_len, D
#     neg_src_key_padding_mask = torch.nn.utils.rnn.pad_sequence([torch.ones(l) for l in neg.traj_len],
#                                                              batch_first=True,
#                                                              padding_value=0).eq(0)
#     neg_src_key_padding_mask = neg_src_key_padding_mask.to(config.device)
    
    
    similarity_logits = perm_proj(anc_trajemb, pos_trajemb, neg_trajemb, 
                anc_src_key_padding_mask=None,
                pos_src_key_padding_mask=None,
                neg_src_key_padding_mask=None,
               ) # N,D=32
    # (N,)
#     pos_dist = torch.sum(torch.square(anc_perm_input-pos_perm_input),dim=1)
#     neg_dist = torch.sum(torch.square(anc_perm_input-neg_perm_input),dim=1)
#     loss_perm = torch.sum(torch.max(pos_dist-neg_dist+0.4, torch.tensor(0., dtype=torch.float32).to(config.device))) / pos_dist.size(0)
    # to perm_proj(l_perm_input, r_perm_input)
#     similarity_logits = perm_proj(l_perm_input, r_perm_input, 
#                                   l_src_key_padding_mask=l_src_key_padding_mask,
#                                   r_src_key_padding_mask=r_src_key_padding_mask,
#                                  ) # (N, 3) or (N, 1)
    loss_perm = criterion_ce(similarity_logits, neg.y.to(config.device))
#     loss_perm = criterion_ce(nn.Sigmoid()(similarity_logits), left.y.to(config.device, torch.float32))
    return loss_perm, similarity_logits, neg.y



