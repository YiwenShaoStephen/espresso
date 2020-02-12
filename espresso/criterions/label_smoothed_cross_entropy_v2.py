# Copyright (c) Yiming Wang
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import numpy as np

import torch

from fairseq import utils
from fairseq.criterions import register_criterion
from fairseq.criterions.label_smoothed_cross_entropy import LabelSmoothedCrossEntropyCriterion
from fairseq.data import data_utils


logger = logging.getLogger(__name__)


def temporal_label_smoothing_prob_mask(
    lprobs: torch.Tensor,  # R[Batch, SeqLength, Vocab]
    target: torch.Tensor,  # Z[Batch, SeqLength]
    padding_index: int = 0,
):
    # see https://arxiv.org/pdf/1612.02695.pdf
    # prob_mask.dtype=int for deterministic behavior of Tensor.scatter_add_()
    prob_mask = torch.zeros_like(lprobs, dtype=torch.int)  # bsz x tgtlen x vocab_size
    idx_tensor = target.new_full(target.size(), padding_index).unsqueeze(-1)  # bsz x tgtlen x 1
    # hard-code the remaining probabilty mass distributed symmetrically
    # over neighbors at distance ±1 and ±2 with a 5 : 2 ratio
    idx_tensor[:, 2:, 0] = target[:, :-2]  # two neighbors to the left
    prob_mask.scatter_add_(-1, idx_tensor, prob_mask.new([2]).expand_as(idx_tensor))
    idx_tensor.fill_(padding_index)[:, 1:, 0] = target[:, :-1]
    prob_mask.scatter_add_(-1, idx_tensor, prob_mask.new([5]).expand_as(idx_tensor))
    idx_tensor.fill_(padding_index)[:, :-2, 0] = target[:, 2:]  # two neighbors to the right
    prob_mask.scatter_add_(-1, idx_tensor, prob_mask.new([2]).expand_as(idx_tensor))
    idx_tensor.fill_(padding_index)[:, :-1, 0] = target[:, 1:]
    prob_mask.scatter_add_(-1, idx_tensor, prob_mask.new([5]).expand_as(idx_tensor))
    prob_mask[:, :, padding_index] = 0  # clear cumulative count on <pad>
    prob_mask = prob_mask.float()  # convert to float
    sum_prob = prob_mask.sum(-1, keepdim=True)
    sum_prob[sum_prob.squeeze(-1).eq(0.)] = 1.  # to deal with the "division by 0" problem
    prob_mask = prob_mask.div_(sum_prob).view(-1, prob_mask.size(-1))
    return prob_mask


def label_smoothed_nll_loss(
    lprobs, target, epsilon, ignore_index=None, reduce=True,
    smoothing_type='uniform', prob_mask=None, unigram_tensor=None,
):
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    if smoothing_type == 'temporal':
        assert torch.is_tensor(prob_mask)
        smooth_loss = -lprobs.mul(prob_mask).sum(-1, keepdim=True)
    elif smoothing_type == 'unigram':
        assert torch.is_tensor(unigram_tensor)
        smooth_loss = -lprobs.matmul(unigram_tensor.to(lprobs))
    elif smoothing_type == 'uniform':
        smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    else:
        raise ValueError('Unsupported smoothing type: {}'.format(smoothing_type))
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        if pad_mask.any():
            nll_loss.masked_fill_(pad_mask, 0.)
            smooth_loss.masked_fill_(pad_mask, 0.)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)
    if reduce:
        nll_loss = nll_loss.sum()
        smooth_loss = smooth_loss.sum()
    eps_i = epsilon / lprobs.size(-1) if smoothing_type == 'uniform' else epsilon
    loss = (1. - epsilon) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


@register_criterion('label_smoothed_cross_entropy_v2')
class LabelSmoothedCrossEntropyV2Criterion(LabelSmoothedCrossEntropyCriterion):

    def __init__(self, args, task):
        super().__init__(args, task)

        self.dictionary = task.target_dictionary
        self.num_updates = -1
        self.epoch = 0
        self.unigram_tensor = None
        if args.smoothing_type == 'unigram':
            self.unigram_tensor = torch.cuda.FloatTensor(self.dictionary.count).unsqueeze(-1) \
                if torch.cuda.is_available() and not args.cpu \
                else torch.FloatTensor(self.dictionary.count).unsqueeze(-1)
            self.unigram_tensor += args.unigram_pseudo_count  # for further backoff
            self.unigram_tensor.div_(self.unigram_tensor.sum())

    @staticmethod
    def add_args(parser):
        """Add criterion-specific arguments to the parser."""
        # fmt: off
        LabelSmoothedCrossEntropyCriterion.add_args(parser)
        parser.add_argument('--print-training-sample-interval', type=int,
                            metavar='N', dest='print_interval', default=500,
                            help='print a training sample (reference + '
                                 'prediction) every this number of updates')
        parser.add_argument('--smoothing-type', type=str, default='uniform',
                            choices=['uniform', 'unigram', 'temporal'],
                            help='label smoothing type. Default: uniform')
        parser.add_argument('--unigram-pseudo-count', type=float, default=1.0,
                            metavar='C', help='pseudo count for unigram label '
                            'smoothing. Only relevant if --smoothing-type=unigram')
        # fmt: on

    def forward(self, model, sample, reduce=True):
        """Compute the loss for the given sample; periodically print out
        randomly sampled predictions from the training set.

        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """
        net_output = model(**sample['net_input'], epoch=self.epoch)
        loss, nll_loss, lprobs = self.compute_loss(
            model, net_output, sample, reduce=reduce, smoothing_type=self.args.smoothing_type
        )
        sample_size = sample['target'].size(0) if self.args.sentence_avg else sample['ntokens']
        logging_output = {
            'loss': loss.data,
            'nll_loss': nll_loss.data,
            'ntokens': sample['ntokens'],
            'nsentences': sample['target'].size(0),
            'sample_size': sample_size,
        }

        if (
            model.training and self.num_updates // self.args.print_interval >
            (self.num_updates - 1) // self.args.print_interval
        ):  # print a randomly sampled result every print_interval updates
            target = model.get_targets(sample, net_output)
            pred = lprobs.argmax(-1).cpu()  # bsz x len
            assert pred.size() == target.size()
            with data_utils.numpy_seed(self.num_updates):
                i = np.random.randint(0, len(sample['id']))
            ref_tokens = sample['target_raw_text'][i]
            length = utils.strip_pad(target.data[i], self.padding_idx).size(0)
            ref_one = self.dictionary.tokens_to_sentence(
                ref_tokens, use_unk_sym=False, bpe_symbol=self.args.remove_bpe,
            )
            pred_one = self.dictionary.tokens_to_sentence(
                self.dictionary.string(pred.data[i][:length]), use_unk_sym=True,
                bpe_symbol=self.args.remove_bpe,
            )
            logger.info('sample REF: ' + ref_one)
            logger.info('sample PRD: ' + pred_one)

        return loss, sample_size, logging_output

    def compute_loss(
        self, model, net_output, sample, reduce=True, smoothing_type='uniform'
    ):
        lprobs = model.get_normalized_probs(net_output, log_probs=True)
        target = model.get_targets(sample, net_output)
        prob_mask = temporal_label_smoothing_prob_mask(
            lprobs, target, padding_index=self.padding_idx,
        ) if smoothing_type == 'temporal' else None
        loss, nll_loss = label_smoothed_nll_loss(
            lprobs.view(-1, lprobs.size(-1)), target.view(-1, 1), self.eps,
            ignore_index=self.padding_idx, reduce=reduce,
            smoothing_type=smoothing_type, prob_mask=prob_mask,
            unigram_tensor=self.unigram_tensor,
        )
        return loss, nll_loss, lprobs

    def set_num_updates(self, num_updates):
        self.num_updates = num_updates

    def set_epoch(self, epoch):
        self.epoch = epoch
