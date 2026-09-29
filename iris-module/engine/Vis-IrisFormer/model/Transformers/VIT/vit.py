""" Vision Transformer (ViT) in PyTorch
"""
from functools import partial

import torch
import torch.nn as nn

from model.Transformers.VIT.layers.patch_embd import PatchEmbed, PositionEmbed
from model.Transformers.VIT.layers.mlp import Mlp
from model.Transformers.VIT.layers.drop import DropPath
from model.Transformers.VIT.layers.weight_init import trunc_normal_
from model.Transformers.VIT.utils.mask_embeeding import MaskEmbeeding, UnMaskEmbeeding, MaskEmbeedingFix
from model.Transformers.VIT.rope import RoPE1D, RoPE2D

import pdb


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, attn_drop=0., proj_drop=0., 
                 pos_embed='none', window_size=(0,0), cls_token=0):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        self.pos_embed = pos_embed
        self.window_size = window_size
        self.cls_token = cls_token

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)


        if self.pos_embed == 'window' or self.pos_embed=='polar':
            # get pair-wise relative position index for each token inside the window
            # codes borrowed from swin transformer
                    
            # define a parameter table of relative position bias
            # the last weight is left for the cls token, if any
            self.relative_position_bias_table = nn.Parameter(torch.zeros((2*self.window_size[0] - 1)*(2*self.window_size[1] - 1)+self.cls_token, num_heads))  # 2*Wh-1 * 2*Ww-1, nH
            nn.init.trunc_normal_(self.relative_position_bias_table, std=.02)

            seq_len = window_size[0]*window_size[1] + self.cls_token
            
            coords_h = torch.arange(self.window_size[0])
            coords_w = torch.arange(self.window_size[1])
            coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
            coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
            relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
            if self.pos_embed=='polar':
                relative_col = torch.where(relative_coords[1,...]<0, window_size[1]-torch.abs(relative_coords[1,...]), relative_coords[1,...])
                relative_coords[1,...] = relative_col

            relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
            relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
            relative_coords[:, :, 1] += self.window_size[1] - 1
            relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
            relative_position_index = relative_coords.sum(-1)
            if self.cls_token>0:
                relative_position_index_cls = torch.zeros(seq_len, seq_len)
                relative_position_index_cls[cls_token:, cls_token:] = relative_position_index
                relative_position_index_cls[:cls_token] = -1
                relative_position_index_cls[:, :cls_token] = -1
                relative_position_index = relative_position_index_cls.to(torch.int64)
            self.register_buffer("relative_position_index", relative_position_index)
        
        elif pos_embed == 'rope1d':
            self.rope = RoPE1D(seq_len=window_size[0]*window_size[1], embedding_dim=head_dim)
        elif pos_embed == 'rope2d':
            self.rope = RoPE2D(window_size=window_size, embedding_dim=head_dim)


    def forward(self, x, mask_index=None):

        B, N, C = x.shape
        cls_token = N-self.window_size[0]*self.window_size[1]
        if mask_index is not None: cls_token += len(mask_index)
        assert cls_token==self.cls_token

        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)   # make torchscript happy (cannot use tensor as tuple)

        if self.pos_embed == 'window' or self.pos_embed=='polar':
            attn = (q @ k.transpose(-2, -1)) * self.scale
            mask_position = [i for i in range(self.relative_position_index.size(0))]
            token_len = self.window_size[0] * self.window_size[1]
            if mask_index is not None:
                token_len -= len(mask_index)
                for midx in mask_index: mask_position.remove(midx+cls_token)

            masked_index = self.relative_position_index[mask_position][:, mask_position]

            relative_position_bias = self.relative_position_bias_table[masked_index.view(-1)].view(N, N, -1)  # Wh*Ww,Wh*Ww,nH
            relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
            attn = attn + relative_position_bias.unsqueeze(0)
        
        elif self.pos_embed=='rope1d' or self.pos_embed=='rope2d':
            q_rotated = self.rope(q, cls_token=self.cls_token, mask_index=mask_index, add='qk')
            k_rotated = self.rope(k, cls_token=self.cls_token, mask_index=mask_index, add='qk')
            attn = (q_rotated @ k_rotated.transpose(-2, -1)) * self.scale
        
        else:
            attn = (q @ k.transpose(-2, -1)) * self.scale
        
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        out = attn @ v

        x = out.transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, 
                 pos_embed='none', window_size=(0,0), cls_token=0):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(dim, num_heads=num_heads, qkv_bias=qkv_bias, attn_drop=attn_drop, proj_drop=drop, pos_embed=pos_embed, window_size=window_size, cls_token=cls_token)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x, mask_index=None):
        x = x + self.drop_path(self.attn(self.norm1(x), mask_index=mask_index))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
    
        return x


class VisionTransformer(nn.Module):
    """ Vision Transformer

    A PyTorch impl of : `An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale`
        - https://arxiv.org/abs/2010.11929

    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=True, drop_rate=0., attn_drop_rate=0., drop_path_rate=0., 
                 embed_layer=PatchEmbed, pos_embed="cosine", norm_layer=nn.LayerNorm, act_layer=nn.GELU, pool='mean',
                 classification=False, vit_type="encoder", mask_ratio=0.75, MAE=True, bottleneck=False, bottleneck_dim=768
                 ):
        """
        Args:
            img_size (int, tuple): input image size
            patch_size (int, tuple): patch size
            in_chans (int): number of input channels
            num_classes (int): number of classes for classification head
            embed_dim (int): embedding dimension
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            embed_layer (nn.Module): patch embedding layer
            pos_embed (nn.Module): position embeeding layer cosine or learnable parameters
            norm_layer: (nn.Module): normalization layer
            pool: 'mean' or 'cls' for classification
            classification: True or False 
            vit_type: "encoder" or "decoder" for MAE
            mask_ratio: a ratio for mask patch numbers
            MAE: Use MAE for trainig 
        """
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        self.num_tokens = 1 if pool=='cls' else 0
        self.classification = classification 
        self.mask_ratio = mask_ratio 
        self.vit_type = vit_type 
        self.MAE = MAE
        
        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        act_layer = act_layer or nn.GELU
    
        self.patch_embed = embed_layer(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches
        print('gird shape in model:', self.patch_embed.grid_size)

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        
        self.pos_embed = torch.zeros([1, num_patches + self.num_tokens, embed_dim])
        if pos_embed == "cosine":
            self.pos_embed = PositionEmbed(num_patches, embed_dim, self.num_tokens)()
        elif pos_embed == 'learnable':
            self.pos_embed = nn.Parameter(torch.randn(1, num_patches + self.num_tokens, embed_dim))
        print('using position embedding:', pos_embed)


        self.dropout = nn.Dropout(p=drop_rate)

        if self.vit_type == "decoder":
            self.unmask_embed = UnMaskEmbeeding(img_size, 
                                           embed_dim,
                                           in_chans,
                                           patch_size,
                                           num_patches
                                           )
        
        if self.MAE:
            dpr = [0.0 for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        else:
            dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]
        
        self.blocks = nn.ModuleList([
            Block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, drop=drop_rate,
                  attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, act_layer=act_layer, 
                  pos_embed=pos_embed, window_size=self.patch_embed.grid_size, cls_token=self.num_tokens)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
        
        self.pool = pool

        self.bottleneck = False
        if bottleneck:
            self.bottleneck = True
            self.bottleneck_mu = nn.Linear(in_features=embed_dim, out_features=bottleneck_dim)
            self.bottleneck_logvar = nn.Linear(in_features=embed_dim, out_features=bottleneck_dim)
            self.bottleneck_norm_mu = norm_layer(bottleneck_dim)
            self.bottleneck_norm_var = norm_layer(bottleneck_dim)

        if self.classification:
            self.class_head = nn.Linear(self.num_features, self.num_classes)
        
        self.apply(self._init_vit_weights)

    def _init_vit_weights(self, module):
        """ ViT weight initialization
        """
        if isinstance(module, nn.Linear):
            if module.out_features == self.num_classes:
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
            else:
                trunc_normal_(module.weight, std=.02)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

        elif isinstance(module, nn.Conv2d):
            nn.init.xavier_uniform_(module.weight)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
                
        elif isinstance(module, (nn.LayerNorm, nn.GroupNorm, nn.BatchNorm2d)):
            nn.init.zeros_(module.bias)
            nn.init.ones_(module.weight)
    
    def _reparameterize(self, mu, logvar):
        std = torch.exp(logvar).sqrt()
        epsilon = torch.randn_like(std)
        sampler = epsilon * std
        return (mu + sampler, sampler)

    def autoencoder(self, x, train=False, mask_index=None):
        """encoder the no mask patch embeeding with position embeeding
        """
        x = self.patch_embed(x)
        if self.num_tokens>0:
            # add cls token for classification
            # no cls token for relative position embedding
            dummpy_token = self.cls_token.expand(x.shape[0], -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
            x = torch.cat((dummpy_token, x), dim=1) 
        x = x + self.pos_embed.to(x.device)
        
        x = MaskEmbeedingFix(x, mask_index_list=mask_index, cls_index=(False if self.num_tokens==0 else True))
        sample_index=None
        
        for block in self.blocks:
            x = block(x, mask_index)
        x = self.norm(x)

        #pdb.set_trace()
        if self.bottleneck:
            mu = self.bottleneck_mu(x)
            mu_norm = self.bottleneck_norm_mu(mu)

            if train:
                logvar = self.bottleneck_logvar(x)
                logvar_norm = self.bottleneck_norm_var(logvar)
                output, _ = self._reparameterize(mu_norm, logvar_norm)
            else: 
                logvar_norm = None
                output = mu_norm
        else:
            output = x
            mu_norm = None
            logvar_norm = None

        return output, sample_index, mask_index, mu_norm, logvar_norm
    
    def encoder_fix_mask(self, x, mask_index_list=None, cls_index=True):
        x = self.patch_embed(x)
        if self.num_tokens>0:
            # add cls token for classification
            dummpy_token = self.cls_token.expand(x.shape[0], -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
            x = torch.cat((dummpy_token, x), dim=1) 
        x = x + self.pos_embed.to(x.device)
        
        # mask the patchemb&posemb
        # cls index: if True, regard the first token as cls token
        x = MaskEmbeedingFix(x, mask_index_list, cls_index)
        
        #x = self.blocks(mask_patch_embeeding)
        for block in self.blocks:
            x = block(x, mask_index=mask_index_list)
        norm_embeeding = self.norm(x)
        return norm_embeeding
    
    def encoder_recon_fix_mask(self, x, sample_index_list, mask_index_list):
        #pdb.set_trace()
        x = self.patch_embed(x)
        # add cls token for classification
        dummpy_token = self.cls_token.expand(x.shape[0], -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
        x = torch.cat((dummpy_token, x), dim=1) 
        x = x + self.pos_embed
        
        # mask the patchemb&posemb
        mask_patch_embeeding = x[:, sample_index_list, :]
        
        x = self.blocks(mask_patch_embeeding)
        norm_embeeding = self.norm(x)
        return norm_embeeding
        
    
    
    def decoder(self, x, sample_index, mask_index):
        """decoder the all patch embeeding with the mask and position embeeding 
        """
        # unmask the patch embeeidng with the encoder embeeding 
        decoder_embed = self.unmask_embed(x, sample_index, mask_index)
        x = decoder_embed + self.pos_embed 
        
        # decoder
        decoder_embeeding = self.blocks(x)
        return decoder_embeeding
    
    
    def forward_features(self, x):
        """Return the layernormalization features
        """
        x = self.patch_embed(x)
        # add cls token for classification
        cls_token = self.cls_token.expand(x.shape[0], -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
        x = torch.cat((cls_token, x), dim=1)
        
        x = self.dropout(x + self.pos_embed)
        x = self.blocks(x)
        x = self.norm(x)
        
        return x 

    def forward(self, x):
        x = self.forward_features(x)
        if self.pool == "mean":
            x = x.mean(dim=1)
        elif self.pool == "cls":
            x = x[:, 0]  # cls token
        else:
            raise ValueError("pool must be 'cls' or 'mean' ")
        
        assert x.shape[1] == self.num_features, "outputs must be same with the features"
        if self.classification:
            x = self.class_head(x)
        return x



def vit_tiny_patch16_224(pretrained=False, **kwargs):
    """ ViT-Tiny (Vit-Ti/16)
    """
    model_kwargs = dict(patch_size=16, embed_dim=192, depth=12, num_heads=3, **kwargs)
    model = VisionTransformer(img_size=224, 
                              in_chans=3,
                              num_classes=1000,
                              mlp_ratio=4., 
                              qkv_bias=True,
                              embed_layer=PatchEmbed, 
                              pos_embed="cosine", 
                              norm_layer=nn.LayerNorm, 
                              act_layer=nn.GELU, 
                              pool='mean',
                              **model_kwargs
                              )
    return model


def vit_small_patch16_224(pretrained=False, **kwargs):
    """ ViT-Small (ViT-S/16)
    NOTE I've replaced my previous 'small' model definition and weights with the small variant from the DeiT paper
    """
    model_kwargs = dict(patch_size=16, embed_dim=384, depth=12, num_heads=6, **kwargs)
    model = VisionTransformer(img_size=224, 
                              in_chans=3,
                              num_classes=1000,
                              mlp_ratio=4., 
                              qkv_bias=True,
                              embed_layer=PatchEmbed, 
                              pos_embed="cosine", 
                              norm_layer=nn.LayerNorm, 
                              act_layer=nn.GELU, 
                              pool='mean',
                              **model_kwargs
                              )
    return model


def vit_base_patch16_224(pretrained=False, **kwargs):
    """ ViT-Base (ViT-B/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 224x224, source https://github.com/google-research/vision_transformer.
    """
    model_kwargs = dict(patch_size=16, embed_dim=768, depth=12, num_heads=12, **kwargs)
    model = VisionTransformer(img_size=224, 
                              in_chans=3,
                              num_classes=1000,
                              mlp_ratio=4., 
                              qkv_bias=True,
                              embed_layer=PatchEmbed, 
                              pos_embed="cosine", 
                              norm_layer=nn.LayerNorm, 
                              act_layer=nn.GELU, 
                              pool='mean',
                              **model_kwargs
                              )
    return model


def vit_large_patch16_224(pretrained=False, **kwargs):
    """ ViT-Large model (ViT-L/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 224x224, source https://github.com/google-research/vision_transformer.
    """
    model_kwargs = dict(patch_size=16, embed_dim=1024, depth=24, num_heads=16, **kwargs)
    model = VisionTransformer(img_size=224, 
                              in_chans=3,
                              num_classes=1000,
                              mlp_ratio=4., 
                              qkv_bias=True,
                              embed_layer=PatchEmbed, 
                              pos_embed="cosine", 
                              norm_layer=nn.LayerNorm, 
                              act_layer=nn.GELU, 
                              pool='mean',
                              **model_kwargs
                              )
    return model


def vit_large_patch16_224_decoder(pretrained=False, **kwargs):
    """ ViT-Large model (ViT-L/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 224x224, source https://github.com/google-research/vision_transformer.
    """
    model_kwargs = dict(patch_size=16, embed_dim=512, depth=8, num_heads=16, **kwargs)
    model = VisionTransformer(img_size=224, 
                              in_chans=3,
                              num_classes=1000,
                              mlp_ratio=4., 
                              qkv_bias=True,
                              embed_layer=PatchEmbed, 
                              pos_embed="cosine", 
                              norm_layer=nn.LayerNorm, 
                              act_layer=nn.GELU, 
                              pool='mean',
                              **model_kwargs
                              )
    return model



if __name__ == '__main__':
    model = vit_large_patch16_224()
    print(model)
    inputs = torch.randn(1, 3, 224, 224)
    outputs = model(inputs)
    print(outputs.shape)
    
