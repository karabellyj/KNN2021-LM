import math
import copy
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.functional as F


class ScaledDotProductAttention(nn.Module):
    def __init__(self, d_k, attn_pdrop):
        super().__init__()
        self.d_k = d_k

        self.dropout = nn.Dropout(attn_pdrop)

    def forward(self, q, k, v, attn_mask):
        # |q| : (batch_size, n_heads, q_len, d_k)
        # |k| : (batch_size, n_heads, k_len, d_k)
        # |v| : (batch_size, n_heads, v_len, d_v)
        # |attn_mask| : (batch_size, n_heads, q_len, k_len)

        attn_score = torch.matmul(q, k.transpose(-1, -2)) / (self.d_k ** 0.5)
        attn_score.masked_fill_(attn_mask, -1e9)
        # |attn_scroe| : (batch_size, n_heads, q_len, k_len)

        attn_weights = nn.Softmax(dim=-1)(attn_score)
        attn_weights = self.dropout(attn_weights)
        # |attn_weights| : (batch_size, n_heads, q_len, k_len)

        output = torch.matmul(attn_weights, v)
        # |output| : (batch_size, n_heads, q_len, d_v)

        return output, attn_weights


class MultiHeadAttention(nn.Module):
    def __init__(self, d_model, n_heads, attn_pdrop):
        super().__init__()
        self.n_heads = n_heads
        self.d_k = self.d_v = d_model // n_heads

        self.WQ = nn.Linear(d_model, d_model)
        self.WK = nn.Linear(d_model, d_model)
        self.WV = nn.Linear(d_model, d_model)
        self.scaled_dot_product_attn = ScaledDotProductAttention(self.d_k, attn_pdrop)
        self.linear = nn.Linear(n_heads * self.d_v, d_model)

    def forward(self, Q, K, V, attn_mask):
        # |Q| : (batch_size, q_len(=seq_len), d_model)
        # |K| : (batch_size, k_len(=seq_len), d_model)
        # |V| : (batch_size, v_len(=seq_len), d_model)
        # |attn_mask| : (batch_size, q_len, k_len)
        batch_size = Q.size(0)

        q_heads = self.WQ(Q).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        k_heads = self.WK(K).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        v_heads = self.WV(V).view(batch_size, -1, self.n_heads, self.d_v).transpose(1, 2)
        # |q_heads| : (batch_size, n_heads, q_len, d_k), |k_heads| : (batch_size, n_heads, k_len, d_k), |v_heads| : (batch_size, n_heads, v_len, d_v)

        attn_mask = attn_mask.unsqueeze(1).repeat(1, self.n_heads, 1, 1)
        # |attn_mask| : (batch_size, n_heads, q_len, k_len)
        attn, attn_weights = self.scaled_dot_product_attn(q_heads, k_heads, v_heads, attn_mask)
        # |attn| : (batch_size, n_heads, q_len, d_v)
        # |attn_weights| : (batch_size, n_heads, q_len, k_len)

        attn = attn.transpose(1, 2).contiguous().view(batch_size, -1, self.n_heads * self.d_v)
        # |attn| : (batch_size, q_len, n_heads * d_v)
        outputs = self.linear(attn)
        # |outputs| : (batch_size, q_len, d_model)

        return outputs, attn_weights


class PositionWiseFeedForwardNetwork(nn.Module):
    def __init__(self, d_model, d_ff):
        super().__init__()

        self.linear1 = nn.Linear(d_model, d_ff)
        self.linear2 = nn.Linear(d_ff, d_model)
        self.gelu = nn.GELU()

        nn.init.normal_(self.linear1.weight, std=0.02)
        nn.init.normal_(self.linear2.weight, std=0.02)

    def forward(self, inputs):
        # |inputs| : (batch_size, seq_len, d_model)

        outputs = self.gelu(self.linear1(inputs))
        # |outputs| : (batch_size, seq_len, d_ff)
        outputs = self.linear2(outputs)
        # |outputs| : (batch_size, seq_len, d_model)

        return outputs


class DecoderLayer(nn.Module):
    def __init__(self, d_model, n_heads, d_ff, attn_pdrop, resid_pdrop, mha=MultiHeadAttention):
        super().__init__()
        self.n_heads = n_heads

        self.mha = mha(d_model, n_heads, attn_pdrop)
        self.dropout1 = nn.Dropout(resid_pdrop)
        self.layernorm1 = nn.LayerNorm(d_model, eps=1e-5)

        self.ffn = PositionWiseFeedForwardNetwork(d_model, d_ff)
        self.dropout2 = nn.Dropout(resid_pdrop)
        self.layernorm2 = nn.LayerNorm(d_model, eps=1e-5)

    def forward(self, inputs, attn_mask):
        # |inputs| : (batch_size, seq_len, d_model)
        # |attn_mask| : (batch_size, seq_len, seq_len)

        attn_outputs, attn_weights = self.mha(inputs, inputs, inputs, attn_mask)
        attn_outputs = self.dropout1(attn_outputs)
        attn_outputs = self.layernorm1(inputs + attn_outputs)
        # |attn_outputs| : (batch_size, seq_len, d_model)
        # |attn_weights| : (batch_size, n_heads, q_len(=seq_len), k_len(=seq_len))

        ffn_outputs = self.ffn(attn_outputs)
        ffn_outputs = self.dropout2(ffn_outputs)
        ffn_outputs = self.layernorm2(attn_outputs + ffn_outputs)
        # |ffn_outputs| : (batch_size, seq_len, d_model)

        return ffn_outputs, attn_weights


class TransformerDecoder(nn.Module):
    def __init__(self, vocab_size, d_model, n_layers, n_heads, d_ff, embd_pdrop, attn_pdrop, resid_pdrop,
                 pad_id, mha=MultiHeadAttention):
        super().__init__()
        self.pad_id = pad_id
        self.d_model = d_model
        
        # layers
        self.embedding = nn.Embedding(vocab_size, d_model)
        self.dropout = nn.Dropout(embd_pdrop)
        #self.pos_embedding = nn.Embedding(seq_len + 1, d_model)
        self.pos_embedding = PositionalEncoding(d_model, embd_pdrop)
        
        self.layers = nn.ModuleList(
            [DecoderLayer(d_model, n_heads, d_ff, attn_pdrop, resid_pdrop, mha=mha) for _ in range(n_layers)])

        nn.init.normal_(self.embedding.weight, std=0.02)

    def forward(self, inputs):
        # |inputs| : (batch_size, seq_len)
        positions = torch.arange(inputs.size(1), device=inputs.device, dtype=inputs.dtype).repeat(inputs.size(0), 1) + 1
        position_pad_mask = inputs.eq(self.pad_id)
        positions.masked_fill_(position_pad_mask, 0)
        # |positions| : (batch_size, seq_len)
        
        outputs = self.embedding(inputs)
        outputs *= torch.sqrt(torch.tensor(self.d_model,dtype=torch.float32))
        outputs = self.pos_embedding(outputs)
        # |outputs| : (batch_size, seq_len, d_model)

        attn_pad_mask = self.get_attention_padding_mask(inputs, inputs, self.pad_id)
        # |attn_pad_mask| : (batch_size, seq_len, seq_len)
        subsequent_mask = self.get_attention_subsequent_mask(inputs).to(device=attn_pad_mask.device)
        # |subsequent_mask| : (batch_size, seq_len, seq_len)
        attn_mask = torch.gt((attn_pad_mask.to(dtype=subsequent_mask.dtype) + subsequent_mask), 0)
        # |attn_mask| : (batch_size, seq_len, seq_len)

        attention_weights = []
        for layer in self.layers:
            outputs, attn_weights = layer(outputs, attn_mask)
            # |outputs| : (batch_size, seq_len, d_model)
            # |attn_weights| : (batch_size, n_heads, seq_len, seq_len)
            attention_weights.append(attn_weights)

        return outputs, attention_weights

    def get_attention_padding_mask(self, q, k, pad_id):
        attn_pad_mask = k.eq(pad_id).unsqueeze(1).repeat(1, q.size(1), 1)
        # |attn_pad_mask| : (batch_size, q_len, k_len)

        return attn_pad_mask

    def get_attention_subsequent_mask(self, q):
        bs, q_len = q.size()
        subsequent_mask = torch.triu(torch.ones(bs, q_len, q_len), diagonal=1)
        # |subsequent_mask| : (batch_size, q_len, q_len)

        return subsequent_mask


class GPT(nn.Module):
    def __init__(self, vocab_size, d_model=768, n_layers=12, n_heads=12, d_ff=3072,
                 embd_pdrop=0.1, attn_pdrop=0.1, resid_pdrop=0.1, pad_id=0, mha=MultiHeadAttention):
        super().__init__()

        self.decoder = TransformerDecoder(vocab_size, d_model, n_layers, n_heads, d_ff,
                                          embd_pdrop, attn_pdrop, resid_pdrop, pad_id, mha=mha)

    def forward(self, inputs):
        # |inputs| : (batch_size, seq_len)

        outputs, attention_weights = self.decoder(inputs)
        # |outputs| : (batch_size, seq_len, d_model)
        # |attention_weights| : [(batch_size, n_heads, seq_len, seq_len)] * n_layers

        return outputs, attention_weights


class LMModel(pl.LightningModule):
    def __init__(self, vocab_size, d_model=768, n_layers=12, n_heads=12, d_ff=3072,
                 embd_pdrop=0.1, attn_pdrop=0.1, resid_pdrop=0.1, pad_id=0, attention='default'):
        super().__init__()
        
        self.save_hyperparameters()

        #pick attention module to use
        if attention == 'default':
            mha=MultiHeadAttention
        elif attention == 'performer':
            from .performer import MultiHeadAttentionPerformer
            mha=MultiHeadAttentionPerformer
        else:
            mha=MultiHeadAttention


        self.gpt = GPT(vocab_size, d_model, n_layers, n_heads, d_ff,
                 embd_pdrop, attn_pdrop, resid_pdrop, pad_id, mha)


        vocab_size, d_model = self.gpt.decoder.embedding.weight.size()

        self.linear = nn.Linear(d_model, vocab_size, bias=False)
        self.linear.weight = self.gpt.decoder.embedding.weight

    def forward(self, inputs):
        # |inputs| : (batch_size, seq_len)

        outputs, attention_weights = self.gpt(inputs)
        # |outputs| : (batch_size, seq_len, d_model)
        # |attention_weights| : [(batch_size, n_heads, seq_len, seq_len)] * n_layers

        lm_logits = self.linear(outputs)
        # |lm_logits| : (batch_size, seq_len, vocab_size)

        return lm_logits, attention_weights

    def configure_optimizers(self):
        
        #optimizer = torch.optim.Adam(self.parameters(),betas=(0.9, 0.98), lr=0.001, eps=1e-09, weight_decay=0.1)
        #scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=0.2, patience=0, threshold=8, cooldown=2)
        optimizer = torch.optim.Adam(self.parameters(), lr=0.0001)
        
        return optimizer #{'optimizer': optimizer, 'lr_scheduler': scheduler,  "monitor": 'perplexity'}

    def training_step(self, batch, batch_idx):
        X, y = batch['inputs_ids'], batch['labels']
        lm_logits, _ = self.forward(X)
        shift_logits = lm_logits[..., :-1, :].contiguous()
        shift_labels = y[..., 1:].contiguous()

        # Flatten the tokens
        loss_fct = nn.CrossEntropyLoss()
        loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
        #loss = loss_fct(lm_logits.view(-1, lm_logits.size(-1)), y.view(-1))
        perplexity = torch.exp(loss)

        self.log('train_perplexity', perplexity, on_step=True, on_epoch=True, prog_bar=True)
        self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        X, y = batch['inputs_ids'], batch['labels']
        lm_logits, _ = self.forward(X)
        shift_logits = lm_logits[..., :-1, :].contiguous()
        shift_labels = y[..., 1:].contiguous()

        # Flatten the tokens
        loss_fct = nn.CrossEntropyLoss()
        loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
        #loss = loss_fct(lm_logits.view(-1, lm_logits.size(-1)), y.view(-1))
        perplexity = torch.exp(loss)

        self.log('val_perplexity', perplexity, on_step=True)
        self.log('val_loss', loss, on_step=True, sync_dist=True)
        return {'val_loss': loss}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        perplexity = torch.exp(avg_loss)

        self.log('val_loss', avg_loss)
        self.log('perplexity', perplexity)
        
class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)      

#if __name__ == '__main__':
#    test = PositionalEncoding(128,0.01)
    
