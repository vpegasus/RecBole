# -*- coding: utf-8 -*-
# @Time   : 2020/10/4 16:55
# @Author : Yujie Lu
# @Email  : yujielu1998@gmail.com


r"""
recbox.model.sequential_recommender.gcsan
################################################

Reference:
Chengfeng Xu et al. "Graph Contextualized Self-Attention Network for Session-based Recommendation." in IJCAI 2019.

"""

import math
import numpy as np

import torch
from torch import nn
from torch.nn import Parameter
from torch.nn import functional as F

from recbox.model.loss import EmbLoss, BPRLoss
from recbox.utils import InputType
from recbox.model.abstract_recommender import SequentialRecommender
from recbox.model.layers import TransformerEncoder


class GNN(nn.Module):
    r"""Graph neural networks are well-suited for session-based recommendation,
    because it can automatically extract features of session graphs with considerations of rich node connections.
    Gated gnn is a neural unit similar to gru.
    """

    def __init__(self, embedding_size, step=1):
        super(GNN, self).__init__()
        self.step = step
        self.embedding_size = embedding_size
        self.input_size = embedding_size * 2
        self.gate_size = embedding_size * 3
        self.w_ih = Parameter(torch.Tensor(self.gate_size, self.input_size))
        self.w_hh = Parameter(torch.Tensor(self.gate_size, self.embedding_size))
        self.b_ih = Parameter(torch.Tensor(self.gate_size))
        self.b_hh = Parameter(torch.Tensor(self.gate_size))

        self.linear_edge_in = nn.Linear(self.embedding_size, self.embedding_size, bias=True)
        self.linear_edge_out = nn.Linear(self.embedding_size, self.embedding_size, bias=True)
        # parameters initialization
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1.0 / math.sqrt(self.embedding_size)
        for weight in self.parameters():
            weight.data.uniform_(-stdv, stdv)


    def GNNCell(self, A, hidden):
        r"""Obtain latent vectors of nodes via graph neural networks.

        Args:
            A(torch.FloatTensor):The connection matrix,shape of [batch_size, max_session_len, 2 * max_session_len]

            hidden(torch.FloatTensor):The item node embedding matrix, shape of [batch_size, max_session_len, embedding_size]

        Returns:
            hy(torch.FloatTensor):Latent vectors of nodes,shape of [batch_size, max_session_len, embedding_size]

        """

        input_in = torch.matmul(A[:, :, :A.size(1)], self.linear_edge_in(hidden))
        input_out = torch.matmul(A[:, :, A.size(1): 2 * A.size(1)], self.linear_edge_out(hidden))
        # [batch_size, max_session_len, embedding_size * 2]
        inputs = torch.cat([input_in, input_out], 2)

        # gi.size equals to gh.size, shape of [batch_size, max_session_len, embdding_size * 3]
        gi = F.linear(inputs, self.w_ih, self.b_ih)
        gh = F.linear(hidden, self.w_hh, self.b_hh)
        # (batch_size, max_session_len, embedding_size)
        i_r, i_i, i_n = gi.chunk(3, 2)
        h_r, h_i, h_n = gh.chunk(3, 2)
        resetgate = torch.sigmoid(i_r + h_r)
        inputgate = torch.sigmoid(i_i + h_i)
        newgate = torch.tanh(i_n + resetgate * h_n)
        hy = (1 - inputgate) * hidden + inputgate * newgate
        return hy

    def forward(self, A, hidden):
        for i in range(self.step):
            hidden = self.GNNCell(A, hidden)
        return hidden

class GCSAN(SequentialRecommender):
    r"""GCSAN captures rich local dependencies via graph nerual network,
     and learns long-range dependencies by applying the self-attention mechanism.
    """
    input_type = InputType.PAIRWISE

    def __init__(self, config, dataset):
        super(GCSAN, self).__init__(config, dataset)

        # load parameters info
        self.embedding_size = config['embedding_size']
        self.hidden_size = config['hidden_size']
        assert self.hidden_size == self.embedding_size
        self.step = config['step']
        self.device = config['device']
        self.weight = config['weight']
        self.reg_weight = config['reg_weight']
        self.loss_type = config['loss_type']
        self.initializer_range = config['initializer_range']

        # define layers and loss
        self.item_embedding = nn.Embedding(self.n_items, self.embedding_size, padding_idx=0)
        self.gnn = GNN(self.embedding_size, self.step)
        self.self_attention = TransformerEncoder(config)
        self.reg_loss = EmbLoss()
        if self.loss_type == 'BPR':
            self.loss_fct = BPRLoss()
        elif self.loss_type == 'CE':
            self.loss_fct = nn.CrossEntropyLoss()
        else:
            raise NotImplementedError("Make sure 'loss_type' in ['BPR', 'CE']!")

        # parameters initialization
        self.apply(self._init_weights)

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.initializer_range)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def get_attention_mask(self, item_seq):
        attention_mask = (item_seq > 0).long()
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)  # torch.int64
        # bidirectional mask
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        return extended_attention_mask

    def get_slice(self, item_seq):
        r"""Get the input needed by the graph neural network

        Returns:
            alias_inputs(torch.LongTensor):The relative coordinates of the item node, shape of [batch_size, max_session_len]
            A(torch.FloatTensor):The connecting matrix, shape of [batch_size, max_session_len, 2 * max_session_len]
            items(torch.LongTensor):The unique item nodes, shape of [batch_size, max_session_len]

        """
        items, n_node, A, alias_inputs = [], [], [], []
        max_n_node = item_seq.size(1)
        item_seq = item_seq.cpu().numpy()

        for u_input in item_seq:
            node = np.unique(u_input)
            items.append(node.tolist() + (max_n_node - len(node)) * [0])
            u_A = np.zeros((max_n_node, max_n_node))
            for i in np.arange(len(u_input) - 1):
                if u_input[i + 1] == 0:
                    break
                u = np.where(node == u_input[i])[0][0]
                v = np.where(node == u_input[i + 1])[0][0]
                u_A[u][v] = 1
            u_sum_in = np.sum(u_A, 0)
            u_sum_in[np.where(u_sum_in == 0)] = 1
            u_A_in = np.divide(u_A, u_sum_in)
            u_sum_out = np.sum(u_A, 1)
            u_sum_out[np.where(u_sum_out == 0)] = 1
            u_A_out = np.divide(u_A.transpose(), u_sum_out)
            u_A = np.concatenate([u_A_in, u_A_out]).transpose()
            A.append(u_A)

            alias_inputs.append([np.where(node == i)[0][0] for i in u_input])

        alias_inputs = torch.LongTensor(alias_inputs).to(self.device)
        A = torch.FloatTensor(A).to(self.device)
        items = torch.LongTensor(items).to(self.device)

        return alias_inputs, A, items


    def forward(self, item_seq, item_seq_len):
        assert self.weight >= 0 and self.weight <= 1
        alias_inputs, A, items = self.get_slice(item_seq)
        hidden = self.item_embedding(items)
        hidden = self.gnn(A, hidden)
        alias_inputs = alias_inputs.view(-1, alias_inputs.size(1), 1).expand(-1, -1, self.embedding_size)
        seq_hidden = torch.gather(hidden, dim=1, index=alias_inputs)
        # fetch the last hidden state of last timestamp
        ht = self.gather_indexes(seq_hidden, item_seq_len - 1)
        a = seq_hidden
        attention_mask = self.get_attention_mask(item_seq)

        outputs = self.self_attention(a, attention_mask, output_all_encoded_layers=True)
        output = outputs[-1]
        at = self.gather_indexes(output, item_seq_len - 1)
        seq_output = self.weight * at + (1 - self.weight) * ht
        return seq_output

    def calculate_loss(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        seq_output = self.forward(item_seq, item_seq_len)
        pos_items = interaction[self.POS_ITEM_ID]
        if self.loss_type == 'BPR':
            neg_items = interaction[self.NEG_ITEM_ID]
            pos_items_emb = self.item_embedding(pos_items)
            neg_items_emb = self.item_embedding(neg_items)
            pos_score = torch.sum(seq_output * pos_items_emb, dim=-1)  # [B]
            neg_score = torch.sum(seq_output * neg_items_emb, dim=-1)  # [B]
            loss = self.loss_fct(pos_score, neg_score)
        else:  # self.loss_type = 'CE'
            test_item_emb = self.item_embedding.weight
            logits = torch.matmul(seq_output, test_item_emb.transpose(0, 1))
            loss = self.loss_fct(logits, pos_items)

        reg_loss = self.reg_loss(self.item_embedding.weight)
        total_loss = loss + reg_loss
        return total_loss

    def predict(self, interaction):
        pass

    def full_sort_predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        seq_output = self.forward(item_seq, item_seq_len)
        test_item_emb = self.item_embedding.weight
        scores = torch.matmul(seq_output, test_item_emb.transpose(0, 1))  # [B, item_num]
        return scores