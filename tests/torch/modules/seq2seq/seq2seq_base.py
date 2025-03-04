# Copyright (c) 2023 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from torch import nn
from torch.nn.functional import log_softmax

PAD = 0


class Seq2Seq(nn.Module):
    """
    Generic Seq2Seq module, with an encoder and a decoder.
    """

    def __init__(self, encoder=None, decoder=None, batch_first=False):
        """
        Constructor for the Seq2Seq module.

        :param encoder: encoder module
        :param decoder: decoder module
        :param batch_first: if True the model uses (batch, seq, feature)
            tensors, if false the model uses (seq, batch, feature) tensors
        """
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.batch_first = batch_first

    def encode(self, inputs, lengths):
        """
        Applies the encoder to inputs with a given input sequence lengths.

        :param inputs: tensor with inputs (batch, seq_len) if 'batch_first'
            else (seq_len, batch)
        :param lengths: vector with sequence lengths (excluding padding)
        """
        return self.encoder(inputs, lengths)

    def decode(self, inputs, context, inference=False):
        """
        Applies the decoder to inputs, given the context from the encoder.

        :param inputs: tensor with inputs (batch, seq_len) if 'batch_first'
            else (seq_len, batch)
        :param context: context from the encoder
        :param inference: if True inference mode, if False training mode
        """
        return self.decoder(inputs, context, inference)

    def generate(self, inputs, context, beam_size):
        """
        Autoregressive generator, works with SequenceGenerator class.
        Executes decoder (in inference mode), applies log_softmax and topK for
        inference with beam search decoding.

        :param inputs: tensor with inputs to the decoder
        :param context: context from the encoder
        :param beam_size: beam size for the generator

        returns: (words, logprobs, scores, new_context)
            words: indices of topK tokens
            logprobs: log probabilities of topK tokens
            scores: scores from the attention module (for coverage penalty)
            new_context: new decoder context, includes new hidden states for
                decoder RNN cells
        """
        logits, scores, new_context = self.decode(inputs, context, True)
        logprobs = log_softmax(logits, dim=-1)
        logprobs, words = logprobs.topk(beam_size, dim=-1)
        return words, logprobs, scores, new_context

    def forward(self, input_encoder, input_enc_len, input_decoder):
        raise NotImplementedError
