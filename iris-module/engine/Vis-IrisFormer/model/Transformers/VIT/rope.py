import torch
import torch.nn as nn
import numpy as np

from typing import Optional, Tuple

'''
This code is inspired by the Pytorch implementation of RoFormer:
https://github.com/JunnYu/RoFormer_pytorch.git

to use RoPE1D, embedding_dim // 2 == 0 is needed
to use RoPE2D, embedding_dim // 4 == 0 is needed  
'''


class RoPEPosionEmbedding(nn.Embedding):
    """This module produces sinusoidal positional embeddings of any length."""

    def __init__(self, num_positions: int, embedding_dim: int, padding_idx: Optional[int] = None):
        ''' embedding dim = head dim, same for all heads   '''
        super().__init__(num_positions, embedding_dim)
        self.weight = self._init_weight(self.weight)

    @staticmethod
    def _init_weight(out: nn.Parameter):
        """
        Identical to the XLM create_sinusoidal_embeddings except features are not interleaved. The cos features are in
        the 2nd half of the vector. [dim // 2:]
        """
        n_pos, dim = out.shape
        position_enc = np.array(
            [
                [pos / np.power(10000, 2 * (j // 2) / dim) for j in range(dim)]
                for pos in range(n_pos)
            ]
        )
        out.requires_grad = False  # set early to avoid an error in pytorch-1.8+
        sentinel = dim // 2 if dim % 2 == 0 else (dim // 2) + 1
        out[:, 0:sentinel] = torch.FloatTensor(np.sin(position_enc[:, 0::2]))
        out[:, sentinel:] = torch.FloatTensor(np.cos(position_enc[:, 1::2]))
        out.detach_()
        return out

    @torch.no_grad()
    def forward(self, positions, seq_len: int, mask_index=None):
        """`input_ids_shape` is expected to be [bsz x seqlen]."""
    
        if mask_index is not None:
            mask = torch.ones(seq_len, dtype=bool)
            mask[mask_index] = 0
            positions = positions[mask]
        
        return super().forward(positions)



class RoPE1D(nn.Module):
    def __init__(self, seq_len:int , embedding_dim:int ):
        super().__init__()
        assert embedding_dim%2==0
        self.rope = RoPEPosionEmbedding(num_positions=seq_len, embedding_dim=embedding_dim)

    def forward(self, x, cls_token=0,  mask_index=None, add='qk'):
        '''seq_len: the original length without mask, dose not contain cls token '''
        seq_len = x.size(2)+len(mask_index)-cls_token if mask_index is not None else x.size(2)-cls_token
        positions = torch.arange(0, seq_len, dtype=torch.long, device=self.rope.weight.device)
        
        sin, cos = self.rope(positions=positions, seq_len=seq_len, mask_index=mask_index)[None, None, :, :].chunk(2, dim=-1)
        x_no_cls = x[:, :, cls_token:, :]
        x1, x2 = x_no_cls[..., 0::2], x_no_cls[..., 1::2]
        if add=='qk':
            x_rotated = torch.cat([x1 * cos - x2 * sin, x2 * cos + x1 * sin], dim=-1)
        elif add=='v':
            x_rotated = torch.stack([x1 * cos - x2 * sin, x2 * cos + x1 * sin], dim=-1).flatten(-2, -1)
        
        if cls_token>0:
            x_rotated = torch.cat((x[:, :, :cls_token, :], x_rotated), dim=2)
        
        return x_rotated


class RoPE2D(nn.Module):
    def __init__(self, window_size: Tuple[int, int], embedding_dim: int):
        ''' embedding dim = head dim, same for all heads   '''
        super().__init__()
        
        assert embedding_dim%4==0
        self.embedding_dim = embedding_dim//2

        self.window_size = window_size
        self.rope_x = RoPEPosionEmbedding(num_positions=window_size[0], embedding_dim=self.embedding_dim)
        self.rope_y = RoPEPosionEmbedding(num_positions=window_size[1], embedding_dim=self.embedding_dim)

        
    def _get_2d_index(self):
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)

        return coords_flatten


    def forward(self, x, cls_token=0, mask_index=None, add='qk'):
        '''seq_len: the original length without mask, dose not contain cls token '''
        seq_len = x.size(2)+len(mask_index)-cls_token if mask_index is not None else x.size(2)-cls_token
        assert seq_len == self.window_size[0]*self.window_size[1]
        
        positions = self._get_2d_index().to(self.rope_x.weight.device)
        sin_x, cos_x = self.rope_x(positions=positions[0], seq_len=seq_len, mask_index=mask_index)[None, None, :, :].chunk(2, dim=-1)
        sin_y, cos_y = self.rope_y(positions=positions[1], seq_len=seq_len, mask_index=mask_index)[None, None, :, :].chunk(2, dim=-1)

        x_no_cls = x[:, :, cls_token:, :]
        
        ''' first half = x, last half = y '''
        x_x = x_no_cls[..., 0:self.embedding_dim]
        x_y = x_no_cls[..., self.embedding_dim:]
        x1_x, x2_x = x_x[..., 0::2], x_x[..., 1::2]
        x1_y, x2_y = x_y[..., 0::2], x_y[..., 1::2]

        if add=='qk':
            x_x_rotated = torch.cat([x1_x * cos_x - x2_x * sin_x, x2_x * cos_x + x1_x * sin_x], dim=-1)
            x_y_rotated = torch.cat([x1_y * cos_y - x2_y * sin_y, x2_y * cos_y + x1_y * sin_y], dim=-1)
        elif add=='v':
            x_x_rotated = torch.stack([x1_x * cos_x - x2_x * sin_x, x2_x * cos_x + x1_x * sin_x], dim=-1).flatten(-2, -1)
            x_y_rotated = torch.stack([x1_y * cos_y - x2_y * sin_y, x2_y * cos_y + x1_y * sin_y], dim=-1).flatten(-2, -1)
               
        x_rotated = torch.cat((x_x_rotated, x_y_rotated), dim=-1)
        if cls_token>0:
            x_rotated = torch.cat((x[:, :, :cls_token, :], x_rotated), dim=2)
        
        return x_rotated
        

if __name__=='__main__':
    x = torch.randn(7,3,8,12)
    cls_token=1
    mask_index = [2,3,4]
    #rope = RoPE(num_positions=10, embedding_dim=6)
    rope = RoPE1D(seq_len=x.size(2)+len(mask_index)-cls_token, embedding_dim=x.size(-1))
    #rope2= RoPE2D(window_size=[2,5], embedding_dim=x.size(-1))

    #x_rope = rope2(x, cls_token=cls_token, mask_index=mask_index)
    x_rope = rope(x, cls_token=cls_token, mask_index=mask_index)
    print(x_rope.size())
