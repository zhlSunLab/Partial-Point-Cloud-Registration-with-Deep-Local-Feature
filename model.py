#!/usr/bin/env python
# -*- coding: utf-8 -*-


import os
import sys
import glob
import h5py
import copy
import math
import json
import numpy as np
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import r2_score
from util import transform_point_cloud, npmat2euler, quat2mat

_EPS = 1e-5 

def clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


def attention(query, key, value, mask=None, dropout=None):
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1).contiguous()) / math.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask==0, -1e9)
    p_attn = F.softmax(scores, dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn


def pairwise_distance(src, tgt):
    inner = -2 * torch.matmul(src.transpose(2, 1).contiguous(), tgt)
    xx = torch.sum(src**2, dim=1, keepdim=True)
    yy = torch.sum(tgt**2, dim=1, keepdim=True)
    distances = xx.transpose(2, 1).contiguous() + inner + yy
    return torch.sqrt(distances)


def knn(x, k):
    inner = -2 * torch.matmul(x.transpose(2, 1).contiguous(), x)
    xx = torch.sum(x ** 2, dim=1, keepdim=True)
    distance = -xx - inner - xx.transpose(2, 1).contiguous()

    idx = distance.topk(k=k, dim=-1)[1]  # (batch_size, num_points, k)
    return idx


def get_graph_feature(x, k=20):
    # x = x.squeeze()
    x = x.view(*x.size()[:3])
    idx = knn(x, k=k)  # (batch_size, num_points, k)
    batch_size, num_points, _ = idx.size()
    device = torch.device('cuda')

    idx_base = torch.arange(0, batch_size, device=device).view(-1, 1, 1) * num_points

    idx = idx + idx_base

    idx = idx.view(-1)

    _, num_dims, _ = x.size()

    x = x.transpose(2, 1).contiguous()  # (batch_size, num_points, num_dims)  -> (batch_size*num_points, num_dims) #   batch_size * num_points * k + range(0, batch_size*num_points)
    feature = x.view(batch_size * num_points, -1)[idx, :]
    feature = feature.view(batch_size, num_points, k, num_dims)
    x = x.view(batch_size, num_points, 1, num_dims).repeat(1, 1, k, 1)

    feature = torch.cat((feature, x, x-feature), dim=3).permute(0, 3, 1, 2)

    return feature


def cycle_consistency(rotation_ab, translation_ab, rotation_ba, translation_ba):
    batch_size = rotation_ab.size(0)
    identity = torch.eye(3, device=rotation_ab.device).unsqueeze(0).repeat(batch_size, 1, 1)
    return F.mse_loss(torch.matmul(rotation_ab, rotation_ba), identity) + F.mse_loss(translation_ab, -translation_ba)


class EncoderDecoder(nn.Module):
    """
    A standard Encoder-Decoder architecture. Base for this and many
    other models.
    """

    def __init__(self, encoder, decoder, src_embed, tgt_embed, generator):
        super(EncoderDecoder, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.generator = generator

    def forward(self, src, tgt, src_mask, tgt_mask):
        "Take in and process masked src and target sequences."
        return self.decode(self.encode(src, src_mask), src_mask,
                           tgt, tgt_mask)

    def encode(self, src, src_mask):
        return self.encoder(self.src_embed(src), src_mask)

    def decode(self, memory, src_mask, tgt, tgt_mask):
        return self.generator(self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask))


class Generator(nn.Module):
    def __init__(self, n_emb_dims):
        super(Generator, self).__init__()
        self.nn = nn.Sequential(nn.Linear(n_emb_dims, n_emb_dims//2),
                                nn.BatchNorm1d(n_emb_dims//2),
                                nn.ReLU(),
                                nn.Linear(n_emb_dims//2, n_emb_dims//4),
                                nn.BatchNorm1d(n_emb_dims//4),
                                nn.ReLU(),
                                nn.Linear(n_emb_dims//4, n_emb_dims//8),
                                nn.BatchNorm1d(n_emb_dims//8),
                                nn.ReLU())
        self.proj_rot = nn.Linear(n_emb_dims//8, 4)
        self.proj_trans = nn.Linear(n_emb_dims//8, 3)

    def forward(self, x):
        x = self.nn(x.max(dim=1)[0])
        rotation = self.proj_rot(x)
        translation = self.proj_trans(x)
        rotation = rotation / torch.norm(rotation, p=2, dim=1, keepdim=True)
        return rotation, translation


class Encoder(nn.Module):
    def __init__(self, layer, N):
        super(Encoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)


class Decoder(nn.Module):
    "Generic N layer decoder with masking."

    def __init__(self, layer, N):
        super(Decoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, memory, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, memory, src_mask, tgt_mask)
        return self.norm(x)


class LayerNorm(nn.Module):
    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x-mean) / (std + self.eps) + self.b_2


class SublayerConnection(nn.Module):
    def __init__(self, size, dropout):
        super(SublayerConnection, self).__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        return x + sublayer(self.norm(x))


class EncoderLayer(nn.Module):
    def __init__(self, size, self_attn, feed_forward, dropout):
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 2)
        self.size = size

    def forward(self, x, mask):
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)


class DecoderLayer(nn.Module):
    "Decoder is made of self-attn, src-attn, and feed forward (defined below)"

    def __init__(self, size, self_attn, src_attn, feed_forward, dropout):
        super(DecoderLayer, self).__init__()
        self.size = size
        self.self_attn = self_attn
        self.src_attn = src_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 3)

    def forward(self, x, memory, src_mask, tgt_mask):
        "Follow Figure 1 (right) for connections."
        m = memory
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, tgt_mask))
        x = self.sublayer[1](x, lambda x: self.src_attn(x, m, m, src_mask))
        return self.sublayer[2](x, self.feed_forward)


class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.0):
        "Take in model size and number of heads."
        super(MultiHeadedAttention, self).__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value, mask=None):
        "Implements Figure 2"
        if mask is not None:
            # Same mask applied to all h heads.
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)

        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = \
            [l(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2).contiguous()
             for l, x in zip(self.linears, (query, key, value))]

        # 2) Apply attention on all the projected vectors in batch.
        x, self.attn = attention(query, key, value, mask=mask,
                                 dropout=self.dropout)

        # 3) "Concat" using a view and apply a final linear.
        x = x.transpose(1, 2).contiguous() \
            .view(nbatches, -1, self.h * self.d_k)
        return self.linears[-1](x)


class PositionwiseFeedForward(nn.Module):
    "Implements FFN equation."
    def __init__(self, d_model, d_ff, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.leaky_relu(self.w_1(x), negative_slope=0.2)))


class PointNet(nn.Module):
    def __init__(self, n_emb_dims=512):
        super(PointNet, self).__init__()
        self.conv1 = nn.Conv1d(3, 64, kernel_size=1, bias=False)
        self.conv2 = nn.Conv1d(64, 64, kernel_size=1, bias=False)
        self.conv3 = nn.Conv1d(64, 64, kernel_size=1, bias=False)
        self.conv4 = nn.Conv1d(64, 128, kernel_size=1, bias=False)
        self.conv5 = nn.Conv1d(128, n_emb_dims, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm1d(64)
        self.bn2 = nn.BatchNorm1d(64)
        self.bn3 = nn.BatchNorm1d(64)
        self.bn4 = nn.BatchNorm1d(128)
        self.bn5 = nn.BatchNorm1d(n_emb_dims)

    def forward(self, x):
        x = F.relu(self.bn1(self.conv1(x)))
        x = F.relu(self.bn2(self.conv2(x)))
        x = F.relu(self.bn3(self.conv3(x)))
        x = F.relu(self.bn4(self.conv4(x)))
        x = F.relu(self.bn5(self.conv5(x)))
        return x


class MLPHead(nn.Module):
    def __init__(self, args):
        super(MLPHead, self).__init__()
        n_emb_dims = args.n_emb_dims
        self.n_emb_dims = n_emb_dims
        self.nn = nn.Sequential(nn.Linear(n_emb_dims*2, n_emb_dims//2),
                                nn.BatchNorm1d(n_emb_dims//2),
                                nn.ReLU(),
                                nn.Linear(n_emb_dims//2, n_emb_dims//4),
                                nn.BatchNorm1d(n_emb_dims//4),
                                nn.ReLU(),
                                nn.Linear(n_emb_dims//4, n_emb_dims//8),
                                nn.BatchNorm1d(n_emb_dims//8),
                                nn.ReLU())
        self.proj_rot = nn.Linear(n_emb_dims//8, 4)
        self.proj_trans = nn.Linear(n_emb_dims//8, 3)

    def forward(self, *input):
        src_embedding = input[0]
        tgt_embedding = input[1]
        embedding = torch.cat((src_embedding, tgt_embedding), dim=1)
        embedding = self.nn(embedding.max(dim=-1)[0])
        rotation = self.proj_rot(embedding)
        rotation = rotation / torch.norm(rotation, p=2, dim=1, keepdim=True)
        translation = self.proj_trans(embedding)
        return quat2mat(rotation), translation


class Identity(nn.Module):
    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, *input):
        return input


class Transformer(nn.Module):
    def __init__(self, args):
        super(Transformer, self).__init__()
        self.n_emb_dims = args.n_emb_dims
        self.N = args.n_blocks
        self.dropout = args.dropout
        self.n_ff_dims = args.n_ff_dims
        self.n_heads = args.n_heads
        c = copy.deepcopy
        attn = MultiHeadedAttention(self.n_heads, self.n_emb_dims)
        ff = PositionwiseFeedForward(self.n_emb_dims, self.n_ff_dims, self.dropout)
        self.model = EncoderDecoder(Encoder(EncoderLayer(self.n_emb_dims, c(attn), c(ff), self.dropout), self.N),
                                    Decoder(DecoderLayer(self.n_emb_dims, c(attn), c(attn), c(ff), self.dropout), self.N),
                                    nn.Sequential(),
                                    nn.Sequential(),
                                    nn.Sequential())

    def forward(self, *input):
        src = input[0]
        tgt = input[1]
        src = src.transpose(2, 1).contiguous()
        tgt = tgt.transpose(2, 1).contiguous()
        tgt_embedding = self.model(src, tgt, None, None).transpose(2, 1).contiguous()
        src_embedding = self.model(tgt, src, None, None).transpose(2, 1).contiguous()
        return src_embedding, tgt_embedding


class TemperatureNet(nn.Module):
    def __init__(self, args):
        super(TemperatureNet, self).__init__()
        self.n_emb_dims = args.n_emb_dims
        self.temp_factor = args.temp_factor
        self.nn = nn.Sequential(nn.Linear(self.n_emb_dims, 128),
                                nn.BatchNorm1d(128),
                                nn.ReLU(),
                                nn.Linear(128, 128),
                                nn.BatchNorm1d(128),
                                nn.ReLU(),
                                nn.Linear(128, 128),
                                nn.BatchNorm1d(128),
                                nn.ReLU(),
                                nn.Linear(128, 1),
                                nn.ReLU())
        self.feature_disparity = None

    def forward(self, *input):
        src_embedding = input[0]
        tgt_embedding = input[1]
        src_embedding = src_embedding.mean(dim=2)
        tgt_embedding = tgt_embedding.mean(dim=2)
        residual = torch.abs(src_embedding-tgt_embedding)

        self.feature_disparity = residual

        return torch.clamp(self.nn(residual), 1.0/self.temp_factor, 1.0*self.temp_factor), residual


class KeyPointNet(nn.Module):
    def __init__(self, num_keypoints):
        super(KeyPointNet, self).__init__()
        self.num_keypoints = num_keypoints

    def forward(self, *input):
        src = input[0]
        tgt = input[1]
        src_embedding = input[2]
        tgt_embedding = input[3]
        batch_size, num_dims, num_points = src_embedding.size()
        src_norm = torch.norm(src_embedding, dim=1, keepdim=True)
        tgt_norm = torch.norm(tgt_embedding, dim=1, keepdim=True)
        src_topk_idx = torch.topk(src_norm, k=self.num_keypoints, dim=2, sorted=False)[1]
        tgt_topk_idx = torch.topk(tgt_norm, k=self.num_keypoints, dim=2, sorted=False)[1]
        src_keypoints_idx = src_topk_idx.repeat(1, 3, 1)
        tgt_keypoints_idx = tgt_topk_idx.repeat(1, 3, 1)
        src_embedding_idx = src_topk_idx.repeat(1, num_dims, 1)
        tgt_embedding_idx = tgt_topk_idx.repeat(1, num_dims, 1)

        src_keypoints = torch.gather(src, dim=2, index=src_keypoints_idx)
        tgt_keypoints = torch.gather(tgt, dim=2, index=tgt_keypoints_idx)
        
        src_embedding = torch.gather(src_embedding, dim=2, index=src_embedding_idx)
        tgt_embedding = torch.gather(tgt_embedding, dim=2, index=tgt_embedding_idx)
        return src_keypoints, tgt_keypoints, src_embedding, tgt_embedding


class ACPNet(nn.Module):
    def __init__(self, args):
        super(ACPNet, self).__init__()
        self.n_emb_dims = args.n_emb_dims
        self.num_keypoints = args.n_keypoints
        self.num_subsampled_points = args.n_subsampled_points
        self.logger = Logger(args)
        if args.emb_nn == 'pointnet':
            self.emb_nn = PointNet(n_emb_dims=self.n_emb_dims)
        elif args.emb_nn == 'FE':
            self.emb_nn = Feature_Extration(n_emb_dims=self.n_emb_dims)
        else:
            raise Exception('Not implemented')

        if args.attention == 'identity':
            self.attention = Identity()
        elif args.attention == 'transformer':
            self.attention = Transformer(args=args)
        else:
            raise Exception("Not implemented")

        self.temp_net = TemperatureNet(args)

        if args.head == 'mlp':
            self.head = MLPHead(args=args)
        elif args.head == 'svd':
            self.head = SVDHead(args=args)
        else:
            raise Exception('Not implemented')

        if self.num_keypoints != self.num_subsampled_points:
            self.keypointnet = KeyPointNet(num_keypoints=self.num_keypoints)
        else:
            self.keypointnet = Identity()
 
    def forward(self, *input):
        src, tgt, src_embedding, tgt_embedding, temperature, feature_disparity = self.predict_embedding(*input)
        rotation_ab, translation_ab = self.head(src_embedding, tgt_embedding, src, tgt, temperature)
        rotation_ba, translation_ba = self.head(tgt_embedding, src_embedding, tgt, src, temperature)
        return rotation_ab, translation_ab, rotation_ba, translation_ba, feature_disparity

    def predict_embedding(self, *input):
        src = input[0]
        tgt = input[1]
        src_embedding = self.emb_nn(src)
        tgt_embedding = self.emb_nn(tgt)

        src_embedding_p, tgt_embedding_p = self.attention(src_embedding, tgt_embedding)

        src_embedding = src_embedding + src_embedding_p
        tgt_embedding = tgt_embedding + tgt_embedding_p

        src, tgt, src_embedding, tgt_embedding = self.keypointnet(src, tgt, src_embedding, tgt_embedding)

        temperature, feature_disparity = self.temp_net(src_embedding, tgt_embedding)

        return src, tgt, src_embedding, tgt_embedding, temperature, feature_disparity

    def predict_keypoint_correspondence(self, *input):
        src, tgt, src_embedding, tgt_embedding, temperature, _ = self.predict_embedding(*input)
        batch_size, num_dims, num_points = src.size()
        d_k = src_embedding.size(1)
        scores = torch.matmul(src_embedding.transpose(2, 1).contiguous(), tgt_embedding) / math.sqrt(d_k)
        scores = scores.view(batch_size*num_points, num_points)
        temperature = temperature.repeat(1, num_points, 1).view(-1, 1)
        scores = F.gumbel_softmax(scores, tau=temperature, hard=True)
        scores = scores.view(batch_size, num_points, num_points)
        return src, tgt, scores


class PRNet(nn.Module):
    def __init__(self, args):
        super(PRNet, self).__init__()
        self.num_iters = args.n_iters
        self.logger = Logger(args)
        self.discount_factor = args.discount_factor
        self.acpnet = ACPNet(args)
        self.model_path = args.model_path
        self.feature_alignment_loss = args.feature_alignment_loss
        self.cycle_consistency_loss = args.cycle_consistency_loss

        if self.model_path is not '':
            self.load(self.model_path)
        if torch.cuda.device_count() > 1:
            self.acpnet = nn.DataParallel(self.acpnet)

    def forward(self, *input):
        rotation_ab, translation_ab, rotation_ba, translation_ba, feature_disparity = self.acpnet(*input)
        return rotation_ab, translation_ab, rotation_ba, translation_ba, feature_disparity

    def predict(self, src, tgt, n_iters=3):
        batch_size = src.size(0)
        rotation_ab_pred = torch.eye(3, device=src.device, dtype=torch.float32).view(1, 3, 3).repeat(batch_size, 1, 1)
        translation_ab_pred = torch.zeros(3, device=src.device, dtype=torch.float32).view(1, 3).repeat(batch_size, 1)
        for i in range(n_iters):
            rotation_ab_pred_i, translation_ab_pred_i, rotation_ba_pred_i, translation_ba_pred_i, _ \
                = self.forward(src, tgt)
            rotation_ab_pred = torch.matmul(rotation_ab_pred_i, rotation_ab_pred)
            translation_ab_pred = torch.matmul(rotation_ab_pred_i, translation_ab_pred.unsqueeze(2)).squeeze(2) \
                                  + translation_ab_pred_i
            src = transform_point_cloud(src, rotation_ab_pred_i, translation_ab_pred_i)

        return rotation_ab_pred, translation_ab_pred

    def _train_one_batch(self, src, tgt, rotation_ab, translation_ab, opt):
        opt.zero_grad()
        batch_size = src.size(0)
        identity = torch.eye(3, device=src.device).unsqueeze(0).repeat(batch_size, 1, 1)

        rotation_ab_pred = torch.eye(3, device=src.device, dtype=torch.float32).view(1, 3, 3).repeat(batch_size, 1, 1)
        translation_ab_pred = torch.zeros(3, device=src.device, dtype=torch.float32).view(1, 3).repeat(batch_size, 1)

        rotation_ba_pred = torch.eye(3, device=src.device, dtype=torch.float32).view(1, 3, 3).repeat(batch_size, 1, 1)
        translation_ba_pred = torch.zeros(3, device=src.device, dtype=torch.float32).view(1, 3).repeat(batch_size, 1)

        total_loss = 0
        total_feature_alignment_loss = 0
        total_cycle_consistency_loss = 0
        total_scale_consensus_loss = 0
        for i in range(self.num_iters):
            rotation_ab_pred_i, translation_ab_pred_i, rotation_ba_pred_i, translation_ba_pred_i, \
            feature_disparity = self.forward(src, tgt)
            rotation_ab_pred = torch.matmul(rotation_ab_pred_i, rotation_ab_pred)
            translation_ab_pred = torch.matmul(rotation_ab_pred_i, translation_ab_pred.unsqueeze(2)).squeeze(2) \
                                  + translation_ab_pred_i

            rotation_ba_pred = torch.matmul(rotation_ba_pred_i, rotation_ba_pred)
            translation_ba_pred = torch.matmul(rotation_ba_pred_i, translation_ba_pred.unsqueeze(2)).squeeze(2) \
                                  + translation_ba_pred_i

            loss = (F.mse_loss(torch.matmul(rotation_ab_pred.transpose(2, 1), rotation_ab), identity) \
                   + F.mse_loss(translation_ab_pred, translation_ab)) * self.discount_factor**i
            feature_alignment_loss = feature_disparity.mean() * self.feature_alignment_loss * self.discount_factor**i
            cycle_consistency_loss = cycle_consistency(rotation_ab_pred_i, translation_ab_pred_i,
                                                       rotation_ba_pred_i, translation_ba_pred_i) \
                                     * self.cycle_consistency_loss * self.discount_factor**i
            scale_consensus_loss = 0
            total_feature_alignment_loss += feature_alignment_loss
            total_cycle_consistency_loss += cycle_consistency_loss
            total_loss = total_loss + loss + feature_alignment_loss + cycle_consistency_loss + scale_consensus_loss
            src = transform_point_cloud(src, rotation_ab_pred_i, translation_ab_pred_i)
        total_loss.backward()
        opt.step()
        return total_loss.item(), total_feature_alignment_loss.item(), total_cycle_consistency_loss.item(), \
               total_scale_consensus_loss, rotation_ab_pred, translation_ab_pred

    def _test_one_batch(self, src, tgt, rotation_ab, translation_ab):
        batch_size = src.size(0)
        identity = torch.eye(3, device=src.device).unsqueeze(0).repeat(batch_size, 1, 1)

        rotation_ab_pred = torch.eye(3, device=src.device, dtype=torch.float32).view(1, 3, 3).repeat(batch_size, 1, 1)
        translation_ab_pred = torch.zeros(3, device=src.device, dtype=torch.float32).view(1, 3).repeat(batch_size, 1)

        rotation_ba_pred = torch.eye(3, device=src.device, dtype=torch.float32).view(1, 3, 3).repeat(batch_size, 1, 1)
        translation_ba_pred = torch.zeros(3, device=src.device, dtype=torch.float32).view(1, 3).repeat(batch_size, 1)

        total_loss = 0
        total_feature_alignment_loss = 0
        total_cycle_consistency_loss = 0
        total_scale_consensus_loss = 0
        for i in range(self.num_iters):
            rotation_ab_pred_i, translation_ab_pred_i, rotation_ba_pred_i, translation_ba_pred_i, \
            feature_disparity = self.forward(src, tgt)
            rotation_ab_pred = torch.matmul(rotation_ab_pred_i, rotation_ab_pred)
            translation_ab_pred = torch.matmul(rotation_ab_pred_i, translation_ab_pred.unsqueeze(2)).squeeze(2) \
                                  + translation_ab_pred_i

            rotation_ba_pred = torch.matmul(rotation_ba_pred_i, rotation_ba_pred)
            translation_ba_pred = torch.matmul(rotation_ba_pred_i, translation_ba_pred.unsqueeze(2)).squeeze(2) \
                                  + translation_ba_pred_i

            loss = (F.mse_loss(torch.matmul(rotation_ab_pred.transpose(2, 1), rotation_ab), identity) \
                    + F.mse_loss(translation_ab_pred, translation_ab)) * self.discount_factor ** i
            feature_alignment_loss = feature_disparity.mean() * self.feature_alignment_loss * self.discount_factor ** i
            cycle_consistency_loss = cycle_consistency(rotation_ab_pred_i, translation_ab_pred_i,
                                                       rotation_ba_pred_i, translation_ba_pred_i) \
                                     * self.cycle_consistency_loss * self.discount_factor ** i
            scale_consensus_loss = 0
            total_feature_alignment_loss += feature_alignment_loss
            total_cycle_consistency_loss += cycle_consistency_loss
            total_loss = total_loss + loss + feature_alignment_loss + cycle_consistency_loss + scale_consensus_loss
            src = transform_point_cloud(src, rotation_ab_pred_i, translation_ab_pred_i)
        return total_loss.item(), total_feature_alignment_loss.item(), total_cycle_consistency_loss.item(), \
               total_scale_consensus_loss, rotation_ab_pred, translation_ab_pred

    def _train_one_epoch(self, epoch, train_loader, opt):
        self.train()
        total_loss = 0
        rotations_ab = []
        translations_ab = []
        rotations_ab_pred = []
        translations_ab_pred = []
        eulers_ab = []
        num_examples = 0
        total_feature_alignment_loss = 0.0
        total_cycle_consistency_loss = 0.0
        total_scale_consensus_loss = 0.0
        for data in tqdm(train_loader):
            src, tgt, rotation_ab, translation_ab, rotation_ba, translation_ba, euler_ab, euler_ba = [d.cuda()
                                                                                                      for d in data]
            loss, feature_alignment_loss, cycle_consistency_loss, scale_consensus_loss,\
            rotation_ab_pred, translation_ab_pred = self._train_one_batch(src, tgt, rotation_ab, translation_ab,
                                                                                opt)
            batch_size = src.size(0)
            num_examples += batch_size
            total_loss = total_loss + loss * batch_size
            total_feature_alignment_loss = total_feature_alignment_loss + feature_alignment_loss * batch_size
            total_cycle_consistency_loss = total_cycle_consistency_loss + cycle_consistency_loss * batch_size
            total_scale_consensus_loss = total_scale_consensus_loss + scale_consensus_loss * batch_size

            rotations_ab.append(rotation_ab.detach().cpu().numpy())
            translations_ab.append(translation_ab.detach().cpu().numpy())
            rotations_ab_pred.append(rotation_ab_pred.detach().cpu().numpy())
            translations_ab_pred.append(translation_ab_pred.detach().cpu().numpy())
            eulers_ab.append(euler_ab.cpu().numpy())
        avg_loss = total_loss / num_examples
        avg_feature_alignment_loss = total_feature_alignment_loss / num_examples
        avg_cycle_consistency_loss = total_cycle_consistency_loss / num_examples
        avg_scale_consensus_loss = total_scale_consensus_loss / num_examples

        rotations_ab = np.concatenate(rotations_ab, axis=0)
        translations_ab = np.concatenate(translations_ab, axis=0)
        rotations_ab_pred = np.concatenate(rotations_ab_pred, axis=0)
        translations_ab_pred = np.concatenate(translations_ab_pred, axis=0)
        eulers_ab = np.degrees(np.concatenate(eulers_ab, axis=0))
        eulers_ab_pred = npmat2euler(rotations_ab_pred)
        r_ab_mse = np.mean((eulers_ab-eulers_ab_pred)**2)
        r_ab_rmse = np.sqrt(r_ab_mse)
        r_ab_mae = np.mean(np.abs(eulers_ab-eulers_ab_pred))
        t_ab_mse = np.mean((translations_ab-translations_ab_pred)**2)
        t_ab_rmse = np.sqrt(t_ab_mse)
        t_ab_mae = np.mean(np.abs(translations_ab-translations_ab_pred))
        r_ab_r2_score = r2_score(eulers_ab, eulers_ab_pred)
        t_ab_r2_score = r2_score(translations_ab, translations_ab_pred)
        info = {'arrow': 'A->B',
                'epoch': epoch,
                'stage': 'train',
                'loss': avg_loss,
                'feature_alignment_loss': avg_feature_alignment_loss,
                'cycle_consistency_loss': avg_cycle_consistency_loss,
                'scale_consensus_loss': avg_scale_consensus_loss,
                'r_ab_mse': r_ab_mse,
                'r_ab_rmse': r_ab_rmse,
                'r_ab_mae': r_ab_mae,
                't_ab_mse': t_ab_mse,
                't_ab_rmse': t_ab_rmse,
                't_ab_mae': t_ab_mae,
                'r_ab_r2_score': r_ab_r2_score,
                't_ab_r2_score': t_ab_r2_score}
        self.logger.write(info)
        return info

    def _test_one_epoch(self, epoch, test_loader):
        self.eval()
        total_loss = 0
        rotations_ab = []
        translations_ab = []
        rotations_ab_pred = []
        translations_ab_pred = []
        eulers_ab = []
        num_examples = 0
        total_feature_alignment_loss = 0.0
        total_cycle_consistency_loss = 0.0
        total_scale_consensus_loss = 0.0
        for data in tqdm(test_loader):
            src, tgt, rotation_ab, translation_ab, rotation_ba, translation_ba, euler_ab, euler_ba = [d.cuda()
                                                                                                      for d in data]
            loss, feature_alignment_loss, cycle_consistency_loss, scale_consensus_loss, \
            rotation_ab_pred, translation_ab_pred = self._test_one_batch(src, tgt, rotation_ab, translation_ab)
            batch_size = src.size(0)
            num_examples += batch_size
            total_loss = total_loss + loss * batch_size
            total_feature_alignment_loss = total_feature_alignment_loss + feature_alignment_loss * batch_size
            total_cycle_consistency_loss = total_cycle_consistency_loss + cycle_consistency_loss * batch_size
            total_scale_consensus_loss = total_scale_consensus_loss + scale_consensus_loss * batch_size

            rotations_ab.append(rotation_ab.detach().cpu().numpy())
            translations_ab.append(translation_ab.detach().cpu().numpy())
            rotations_ab_pred.append(rotation_ab_pred.detach().cpu().numpy())
            translations_ab_pred.append(translation_ab_pred.detach().cpu().numpy())
            eulers_ab.append(euler_ab.cpu().numpy())
        avg_loss = total_loss / num_examples
        avg_feature_alignment_loss = total_feature_alignment_loss / num_examples
        avg_cycle_consistency_loss = total_cycle_consistency_loss / num_examples
        avg_scale_consensus_loss = total_scale_consensus_loss / num_examples

        rotations_ab = np.concatenate(rotations_ab, axis=0)
        translations_ab = np.concatenate(translations_ab, axis=0)
        rotations_ab_pred = np.concatenate(rotations_ab_pred, axis=0)
        translations_ab_pred = np.concatenate(translations_ab_pred, axis=0)
        eulers_ab = np.degrees(np.concatenate(eulers_ab, axis=0))
        eulers_ab_pred = npmat2euler(rotations_ab_pred)
        r_ab_mse = np.mean((eulers_ab - eulers_ab_pred) ** 2)
        r_ab_rmse = np.sqrt(r_ab_mse)
        r_ab_mae = np.mean(np.abs(eulers_ab - eulers_ab_pred))
        t_ab_mse = np.mean((translations_ab - translations_ab_pred) ** 2)
        t_ab_rmse = np.sqrt(t_ab_mse)
        t_ab_mae = np.mean(np.abs(translations_ab - translations_ab_pred))
        r_ab_r2_score = r2_score(eulers_ab, eulers_ab_pred)
        t_ab_r2_score = r2_score(translations_ab, translations_ab_pred)

        info = {'arrow': 'A->B',
                'epoch': epoch,
                'stage': 'test',
                'loss': avg_loss,
                'feature_alignment_loss': avg_feature_alignment_loss,
                'cycle_consistency_loss': avg_cycle_consistency_loss,
                'scale_consensus_loss': avg_scale_consensus_loss,
                'r_ab_mse': r_ab_mse,
                'r_ab_rmse': r_ab_rmse,
                'r_ab_mae': r_ab_mae,
                't_ab_mse': t_ab_mse,
                't_ab_rmse': t_ab_rmse,
                't_ab_mae': t_ab_mae,
                'r_ab_r2_score': r_ab_r2_score,
                't_ab_r2_score': t_ab_r2_score}
        self.logger.write(info)
        return info

    def save(self, path):
        if torch.cuda.device_count() > 1:
            torch.save(self.acpnet.module.state_dict(), path)
        else:
            torch.save(self.acpnet.state_dict(), path)

    def load(self, path):
        self.acpnet.load_state_dict(torch.load(path))
        # S: My stuff
    def get_state(self):
        return self.acpnet.state_dict()

    def set_state(self, state):
        self.acpnet.load_state_dict(state)


class Logger:
    def __init__(self, args):
        self.path = 'checkpoints/' + args.exp_name
        self.fw = open(self.path+'/log', 'a')
        self.fw.write(str(args))
        self.fw.write('\n')
        self.fw.flush()
        print(str(args))
        with open(os.path.join(self.path, 'args.txt'), 'w') as f:
            json.dump(args.__dict__, f, indent=2)

    def write(self, info):
        arrow = info['arrow']
        epoch = info['epoch']
        stage = info['stage']
        loss = info['loss']
        feature_alignment_loss = info['feature_alignment_loss']
        cycle_consistency_loss = info['cycle_consistency_loss']
        scale_consensus_loss = info['scale_consensus_loss']
        r_ab_mse = info['r_ab_mse']
        r_ab_rmse = info['r_ab_rmse']
        r_ab_mae = info['r_ab_mae']
        t_ab_mse = info['t_ab_mse']
        t_ab_rmse = info['t_ab_rmse']
        t_ab_mae = info['t_ab_mae']
        r_ab_r2_score = info['r_ab_r2_score']
        t_ab_r2_score = info['t_ab_r2_score']
        text = '%s:: Stage: %s, Epoch: %d, Loss: %f, Feature_alignment_loss: %f, Cycle_consistency_loss: %f, ' \
               'Scale_consensus_loss: %f, Rot_MSE: %f, Rot_RMSE: %f, ' \
               'Rot_MAE: %f, Rot_R2: %f, Trans_MSE: %f, ' \
               'Trans_RMSE: %f, Trans_MAE: %f, Trans_R2: %f\n' % \
               (arrow, stage, epoch, loss, feature_alignment_loss, cycle_consistency_loss, scale_consensus_loss,
                r_ab_mse, r_ab_rmse, r_ab_mae,
                r_ab_r2_score, t_ab_mse, t_ab_rmse, t_ab_mae, t_ab_r2_score)
        self.fw.write(text)
        self.fw.flush()
        print(text)

    def close(self):
        self.fw.close()


if __name__ == '__main__':
    print('hello world')
    
    
def get_neighbors(x, k=20, idx=None):
    batch_size = x.size(0)
    num_points = x.size(2)
    x = x.view(batch_size, -1, num_points)
    if idx is None:
        idx = knn(x, k=k)
    device = torch.device('cuda')

    idx_base = torch.arange(0, batch_size, device=device).view(-1, 1, 1) * num_points

    idx = idx + idx_base

    idx = idx.view(-1)

    _, num_dims, _ = x.size()

    x = x.transpose(2, 1).contiguous()
    feature = x.view(batch_size * num_points, -1)[idx, :]
    feature = feature.view(batch_size, num_points, k, num_dims)  # b 768 20 3

    return feature


class local_attention_extration(nn.Module):
    def __init__(self):
        super(local_attention_extration, self).__init__()


        self.conv2d1 = nn.Conv2d(3, 16, 1)
        self.conv2d2 = nn.Conv2d(3, 16, 1)
        self.conv2d3 = nn.Conv2d(16, 1, 1)

        self.act1 = torch.nn.LeakyReLU()
        self.act2 = torch.nn.ELU()

    def forward(self, x):

        if len(x.size()) == 3:
            x = x.unsqueeze(3)  # b 3 768 1
        neighbors = get_neighbors(x)  # b 768 20 3
        x = x.permute(0, 2, 3, 1).repeat(1, 1, 20, 1)  # b 768 20 3
        x_n = (x - neighbors).permute(0, 3, 1, 2)  # b 3 768 20
        
        x = x.permute(0, 3, 1, 2)  #b 3 768 20
        new_feature = self.conv2d1(x)  # b 16 768 20
        self_attention = self.conv2d3(new_feature)  # b 1 768 20

        edge_feature = self.conv2d2(x_n)  # b 16 768 20
        x1 = edge_feature.permute(0, 2, 3, 1)  # b 768 20 16

        neibor_attention = self.conv2d3(edge_feature)  # b 1 768 20
        logits = (self_attention + neibor_attention).permute(0, 2, 1, 3)  # b 768 1 20
        coefs = self.act1(logits)  #
        coefs = F.softmax(coefs, dim=-1)  # b 768 1 20  ��attention
        x2 = coefs

        vals = torch.matmul(x2, x1)
        ret = self.act2(vals)  #b 768 1 16
        
        ret = ret.permute(0, 3, 1, 2)#b 16 768 1

        ret = ret.squeeze(-1)
        
        return ret 


class att_pooling(nn.Module):
    def __init__(self, channel):
        nn.Module.__init__(self)
        self.linear = nn.Linear(channel, channel)
        #self.mlp = nn.Conv1d(channel, channel, kernel_size=1, bias=False)
        self.mlp = nn.Sequential(
           # nn.InstanceNorm2d(channel, eps=1e-3),
           nn.BatchNorm1d(channel),
           nn.ReLU(),
           nn.Conv1d(channel, channel, kernel_size=1))
        self.bn = nn.BatchNorm1d(channel)
        self.conv = nn.Sequential(
           # nn.InstanceNorm2d(channel, eps=1e-3),
           nn.BatchNorm2d(channel),
           nn.ReLU(),
           nn.Conv2d(channel, channel, kernel_size=1))
           
           
    def forward(self, x):
           
        x = x.permute(0, 2, 1, 3)#b 768 3 k 
        batch_size, num_points, num_dims, num_neigh = x.size()   
        x_reshape = x.reshape(batch_size*num_points, num_dims, num_neigh) #b*768 3 20
        att_activation = self.mlp(x_reshape) #b*768 3 k
        att_scores = torch.softmax(att_activation, dim=2) #b*768 3 k
        f_agg = x_reshape * att_scores
        f_agg = torch.sum(f_agg, dim=2, keepdim=True) #b*768 3  
        f_agg = f_agg.view(batch_size, num_points, num_dims).permute(0, 2, 1) #b 3 768 
      #  f_agg = self.conv(f_agg)
      
        return f_agg #b 3 768 
        
        
class Feature_Extration(nn.Module):
    def __init__(self, n_emb_dims=512):
        super(Feature_Extration, self).__init__()
    
        self.conv1 = nn.Conv2d(19 * 3, 64, kernel_size=1, bias=False)
        self.conv2 = nn.Conv2d(83 * 3, 256, kernel_size=1, bias=False)
        self.conv3 = nn.Conv1d(512, n_emb_dims, kernel_size=1, bias=False)
      
        self.bn1 = nn.BatchNorm2d(64)
        self.bn2 = nn.BatchNorm2d(256)
        self.bn3 = nn.BatchNorm1d(n_emb_dims)

        self.lafe = local_attention_extration()

        self.pool1 = att_pooling(64)
        self.pool2 = att_pooling(256)
       
        self.mlp = nn.Sequential(
           # nn.InstanceNorm2d(channel, eps=1e-3),
           nn.Conv1d(19, 256, kernel_size=1),
           nn.BatchNorm1d(256),
           nn.ReLU(),
           )

    def forward(self, x):
        batch_size, num_dims, num_points = x.size()  ## 10,3,768
        xm = self.lafe(x)
        x_manet = torch.cat((x, xm), dim=1)  #b 19 768 
        
        x_mlp = self.mlp(x_manet)  #b 256 768
    
        x = get_graph_feature(x_manet)
        x = F.leaky_relu(self.bn1(self.conv1(x)), negative_slope=0.2)
        x_p1 = self.pool1(x) #b 64 768
        
        x_c1 = torch.cat((x_manet, x_p1), dim=1)# b 64+19 768
        
        x = get_graph_feature(x_c1)
        x = F.leaky_relu(self.bn2(self.conv2(x)), negative_slope=0.2)
        x_p2 = self.pool2(x) #b 256 768

        x_c2 = torch.cat((x_mlp, x_p2), dim=1)
        x = F.leaky_relu(self.bn3(self.conv3(x_c2)), negative_slope=0.2)  #b 512 768

        return x   


class PointCN(nn.Module):
    def __init__(self, channels, out_channels=None):
        nn.Module.__init__(self)
        if not out_channels:
            out_channels = channels
        self.shot_cut = None
        if out_channels != channels:
            self.shot_cut = nn.Conv2d(channels, out_channels, kernel_size=1)
        self.conv = nn.Sequential(
            nn.InstanceNorm2d(channels),
            nn.BatchNorm2d(channels),
            nn.ReLU(),
            nn.Conv2d(channels, out_channels, kernel_size=1),
            nn.InstanceNorm2d(out_channels),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(),
            nn.Conv2d(out_channels, out_channels, kernel_size=1)
        )

    def forward(self, x):
        out = self.conv(x)
        if self.shot_cut:
            out = out + self.shot_cut(x)
        else:
            out = out + x
        return out




class trans(nn.Module):
    def __init__(self, dim1, dim2):
        nn.Module.__init__(self)
        self.dim1 = dim1
        self.dim2 = dim2

    def forward(self, x):
        return x.transpose(self.dim1, self.dim2)


class OAFilter(nn.Module):
    def __init__(self, channels, points, out_channels=None):
        nn.Module.__init__(self)
        if not out_channels:
            out_channels = channels
        self.shot_cut = None
        if out_channels != channels:
            self.shot_cut = nn.Conv2d(channels, out_channels, kernel_size=1)
        self.conv1 = nn.Sequential(
            nn.InstanceNorm2d(channels, eps=1e-3),
            nn.BatchNorm2d(channels),
            nn.ReLU(),
            nn.Conv2d(channels, out_channels, kernel_size=1),  # b*c*n*1
            trans(1, 2))

        # Spatial Correlation Layer
        self.conv2 = nn.Sequential(
            nn.BatchNorm2d(points),
            nn.ReLU(),
            nn.Conv2d(points, points, kernel_size=1)
        )
        self.conv3 = nn.Sequential(
            trans(1, 2),
            nn.InstanceNorm2d(out_channels, eps=1e-3),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(),
            nn.Conv2d(out_channels, out_channels, kernel_size=1)
        )

    def forward(self, x):
        out = self.conv1(x)
        out = out + self.conv2(out)
        out = self.conv3(out)
        if self.shot_cut:
            out = out + self.shot_cut(x)
        else:
            out = out + x
        return out


class diff_pool(nn.Module):
    def __init__(self, in_channel, output_points):
        nn.Module.__init__(self)
        self.output_points = output_points
        self.conv = nn.Sequential(
            nn.InstanceNorm2d(in_channel, eps=1e-3),
            nn.BatchNorm2d(in_channel),
            nn.ReLU(),
            nn.Conv2d(in_channel, output_points, kernel_size=1))

    def forward(self, x):
        embed = self.conv(x)  # b*k*n*1   n 32 768 1
        S = torch.softmax(embed, dim=2).squeeze(3)  #b 32 768
        out = torch.matmul(x.squeeze(3), S.transpose(1, 2)).unsqueeze(3)  #b 64 32 1
        return out


class diff_unpool(nn.Module):
    def __init__(self, in_channel, output_points):
        nn.Module.__init__(self)
        self.output_points = output_points
        self.conv = nn.Sequential(
            nn.InstanceNorm2d(in_channel, eps=1e-3),
            nn.BatchNorm2d(in_channel),
            nn.ReLU(),
            nn.Conv2d(in_channel, output_points, kernel_size=1))

    def forward(self, x_up, x_down):
        #x_up: b*c*n*1      b 64 768 1 
        #x_down: b*c*k*1  b 64 32 1 
        embed = self.conv(x_up)  # b*k*n*1  b 32 768 1 
        S = torch.softmax(embed, dim=1).squeeze(3)  # b*k*n   b 32 768 1
        out = torch.matmul(x_down.squeeze(3), S).unsqueeze(3)  #b 64 768 1 
        return out


class SVDHead(nn.Module):
    def __init__(self, args):
        super(SVDHead, self).__init__()
        self.n_emb_dims = args.n_emb_dims
        self.cat_sampler = args.cat_sampler
        self.reflect = nn.Parameter(torch.eye(3), requires_grad=False)
        self.reflect[2, 2] = -1
        self.temperature = nn.Parameter(torch.ones(1)*0.5, requires_grad=True)
        self.my_iter = torch.ones(1)
        self.on_gpu = args.svd_on_gpu
        
        
        channels = 16
        l2_nums = 6
        self.layer_num = 2
        self.conv1 = nn.Conv2d(6, channels, kernel_size=1)

        self.l1_1 = []
        for _ in range(self.layer_num//2):
            self.l1_1.append(PointCN(channels))
        

        self.down1 = diff_pool(channels, l2_nums)

        self.l2 = []
        for _ in range(self.layer_num//2):
            self.l2.append(OAFilter(channels, l2_nums))

        self.up1 = diff_unpool(channels, l2_nums)

        self.l1_2 = []
        self.l1_2.append(PointCN(2*channels, channels))
        for _ in range(self.layer_num//2-1):
            self.l1_2.append(PointCN(channels))

        self.l1_1 = nn.Sequential(*self.l1_1)
        self.l1_2 = nn.Sequential(*self.l1_2)
        self.l2 = nn.Sequential(*self.l2)

        self.output = nn.Conv2d(channels, 1, kernel_size=1)

    def forward(self, *input):
        src_embedding = input[0]        #b 512 768
        tgt_embedding = input[1]        #b 512 768
        src = input[2]              #b 3 768
        tgt = input[3]              #b 3 768
        batch_size, num_dims, num_points = src.size()
        temperature = input[4].view(batch_size, 1, 1)   #b 1 1
        xs = torch.cat((src, tgt), dim=1)  #b 6 768
        #data = xs.view(batch_size, 6, num_points, 1)    #b 6 768 1
        data = xs.unsqueeze(-1)  #b 6 768 1

        x1_1 = self.conv1(data)   #b 32 768 1
        x1_1 = self.l1_1(x1_1)    #b 32 768 1
        x_down = self.down1(x1_1)   #b 32 768 1
        x2 = self.l2(x_down)        #b 32 16 1
        x_up = self.up1(x1_1, x2)   #  b 32 768 1 
        out = self.l1_2(torch.cat([x1_1, x_up], dim=1))  #b 64 768 1 


        logits = torch.squeeze(torch.squeeze(self.output(out), 3), 1) #b 768
        weights = torch.relu(torch.tanh(logits))
        
        
        if torch.any(torch.sum(weights, dim=1) == 0.0):
            weights = weights + 1 / weights.shape[1]
            
        weights = weights.unsqueeze(2)

       
        
        if self.cat_sampler == 'softmax':
            d_k = src_embedding.size(1)
            scores = torch.matmul(src_embedding.transpose(2, 1).contiguous(), tgt_embedding) / math.sqrt(d_k)
            scores = torch.softmax(temperature*scores, dim=2)
        elif self.cat_sampler == 'gumbel_softmax':
            d_k = src_embedding.size(1)   #512
            scores = torch.matmul(src_embedding.transpose(2, 1).contiguous(), tgt_embedding) / math.sqrt(d_k)
            scores = scores.view(batch_size*num_points, num_points)
            temperature = temperature.repeat(1, num_points, 1).view(-1, 1)
            scores = F.gumbel_softmax(scores, tau=temperature, hard=True)
            scores = scores.view(batch_size, num_points, num_points)
        else:
            raise Exception('not implemented')
        
        src = src.transpose(1, 2)    #b 768 3
        srcm = torch.matmul(weights.transpose(1, 2), src) / (torch.sum(weights, dim=1).unsqueeze(1) + _EPS)
        src_centered = src - srcm    #b 768 3
        src_centered = src_centered.transpose(1, 2)   #b 3 768
        tgt = torch.matmul(tgt, scores.transpose(2, 1).contiguous()) 
        tgt = tgt.transpose(1, 2)    #b 768 3
        tgtm = torch.matmul(weights.transpose(1, 2), tgt) / (torch.sum(weights, dim=1).unsqueeze(1) + _EPS)
        
        tgt_centered = tgt - tgtm
        tgt_centered = tgt_centered.transpose(1, 2)   #b 3 768
        
        
        weight_matrix = torch.diag_embed(weights.squeeze(2))

        if self.on_gpu:
        # H = torch.matmul(src_centered, src_corr_centered.transpose(2, 1).contiguous())
            H = torch.matmul(src_centered,torch.matmul(weight_matrix, tgt_centered.transpose(2, 1).contiguous()))
            # print(H.shape)
            R = torch.zeros((src.size(0), 3, 3)).cuda()



            for i in range(src.size(0)):
                u, s, v = torch.svd(H[i])
                r = torch.matmul(v, u.transpose(1, 0)).contiguous()
                r_det = torch.det(r).item()
                diag = torch.tensor([[1.0, 0, 0],
                                    [0, 1.0, 0],
                                    [0, 0, r_det]]).cuda()
                r = torch.matmul(torch.matmul(v, diag), u.transpose(1, 0)).contiguous()
                R[i] = r
        else:
        ## Original on CPU
            H = torch.matmul(src_centered,torch.matmul(weight_matrix, tgt_centered.transpose(2, 1).contiguous())).to('cpu', copy=True)
            R = []
            for i in range(src.size(0)):
                u, s, v = torch.svd(H[i] + torch.eye(3,) * 1e-7)
                r = torch.matmul(v, u.transpose(1, 0)).contiguous()
                r_det = torch.det(r).item()
                diag = torch.from_numpy(np.array([[1.0, 0, 0],
                                                [0, 1.0, 0],
                                                [0, 0, r_det]]).astype('float32')).to(v.device)
                r = torch.matmul(torch.matmul(v, diag), u.transpose(1, 0)).contiguous()
                R.append(r)
            R = torch.stack(R, dim=0).cuda()
            # t = torch.matmul(-R, src.mean(dim=2, keepdim=True)) + src_corr.mean(dim=2, keepdim=True)
        t = torch.matmul(-R, srcm.transpose(1, 2)) + tgtm.transpose(1, 2)
        if self.training:
            self.my_iter += 1
        return R, t.view(batch_size, 3)