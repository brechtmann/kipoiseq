from collections import OrderedDict
import pandas as pd
import numpy as np
from copy import deepcopy

from kipoi.metadata import GenomicRanges
from kipoi.specs import DataLoaderArgument, ArraySpecialType
from kipoi.plugin import is_installed
from kipoi.data import Dataset, kipoi_dataloader
from kipoi.specs import Author, Dependencies


from kipoiseq.extractors import FastaStringExtractor
from kipoiseq.transforms import SwapAxes, DummyAxis, Compose, OneHot
from kipoiseq.transforms.functional import one_hot, resize_interval
from kipoiseq.utils import get_alphabet, get_onehot_shape, to_scalar

import pybedtools
from pybedtools import BedTool, Interval

# general dependencies
deps = Dependencies(conda=['bioconda::genomelake', 'bioconda::pybedtools', 'bioconda::pyfaidx', 'numpy', 'pandas'],
                    pip=['kipoiseq'])
package_authors = [Author(name='Ziga Avsec', github='avsecz'),
                   Author(name='Roman Kreuzhuber', github='krrome')]


def parse_dtype(dtype):
    dtypes = {'int': int, 'string': str, 'float': float, 'bool': bool}
    if dtype is None:
        return None
    if dtype in list(dtypes.values()):
        return dtype
    if dtype not in dtypes:
        raise Exception("Datatype '{0}' not recognized. Allowed are: {1}".format(dtype, str(list(dtypes.keys()))))
    return dtypes[dtype]


def parse_alphabet(alphabet):
    if isinstance(alphabet, str):
        return alphabet.split('')
    else:
        return alphabet


class BedDataset(object):
    """Reads a tsv file in the following format:
    ```
    chr  start  stop  task1  task2 ...
    ```

    # Arguments
      tsv_file: tsv file type
      bed_columns: number of columns corresponding to the bed file. All the columns
        after that will be parsed as targets
      num_chr: if specified, 'chr' in the chromosome name will be dropped
      label_dtype: specific data type for labels
      ambiguous_mask: if specified, rows containing only ambiguous_mask values will be skipped
      incl_chromosomes: exclusive list of chromosome names to include in the final dataset.
        if not None, only these will be present in the dataset
      excl_chromosomes: list of chromosome names to omit from the dataset.
    """

    # bed types accorging to
    # https://www.ensembl.org/info/website/upload/bed.html
    bed_types = [str,  # chrom
                 int,  # chromStart
                 int,  # chromEnd
                 str,  # name
                 float,  # score
                 str,  # strand
                 int,  # thickStart
                 int,  # thickEnd
                 str,  # itemRbg
                 int,  # blockCount
                 int,  # blockSizes
                 int]  # blockStarts

    def __init__(self, tsv_file,
                 label_dtype=None,
                 bed_columns=3,
                 num_chr=False,
                 ambiguous_mask=None,
                 incl_chromosomes=None,
                 excl_chromosomes=None):
        self.tsv_file = tsv_file
        self.bed_columns = bed_columns
        self.num_chr = num_chr
        self.label_dtype = label_dtype
        self.ambiguous_mask = ambiguous_mask
        self.incl_chromosomes = incl_chromosomes
        self.excl_chromosomes = excl_chromosomes

        df_peek = pd.read_table(self.tsv_file,
                                header=None,
                                nrows=1,
                                sep='\t')
        self.n_tasks = df_peek.shape[1] - self.bed_columns
        assert self.n_tasks >= 0
        self.df = pd.read_table(self.tsv_file,
                                header=None,
                                dtype={i: d
                                       for i, d in enumerate(self.bed_types[:self.bed_columns] +
                                                             [self.label_dtype] * self.n_tasks)},
                                sep='\t')
        if self.num_chr and self.df.iloc[0][0].startswith("chr"):
            self.df[0] = self.df[0].str.replace("^chr", "")
        if not self.num_chr and not self.df.iloc[0][0].startswith("chr"):
            self.df[0] = "chr" + self.df[0]

        if ambiguous_mask is not None:
            # exclude regions where only ambigous labels are present
            self.df = self.df[~np.all(self.df.iloc[:, self.bed_columns:] == ambiguous_mask, axis=1)]

            # omit data outside chromosomes
        if incl_chromosomes is not None:
            self.df = self.df[self.df[0].isin(incl_chromosomes)]
        if excl_chromosomes is not None:
            self.df = self.df[~self.df[0].isin(excl_chromosomes)]

    def __getitem__(self, idx):
        """Returns (pybedtools.Interval, labels)
        """
        row = self.df.iloc[idx]
        interval = pybedtools.create_interval_from_list([to_scalar(x) for x in row.iloc[:self.bed_columns]])

        if self.n_tasks == 0:
            labels = {}
        else:
            labels = row.iloc[self.bed_columns:].values.astype(self.label_dtype)
        return interval, labels

    def __len__(self):
        return len(self.df)

    def get_targets(self):
        return self.df.iloc[:, self.bed_columns:].values.astype(self.label_dtype)


@kipoi_dataloader(override={"dependencies": deps, 'info.authors': package_authors})
class SeqStringDataset(Dataset):
    """
    info:
        doc: >
           Dataloader for a combination of fasta and tab-delimited input files such as bed files. The dataloader extracts
           regions from the fasta file as defined in the tab-delimited `intervals_file`. Returned sequences are of the type
           np.array([str]).
    args:
        intervals_file:
            doc: bed3+<columns> file path containing intervals + (optionally) labels
            example: example_files/intervals_files_ENCSR000EMT_chr21_10000.tsv
        fasta_file:
            doc: Reference genome FASTA file path.
            example: example_files/chr21.fa
        num_chr_fasta:
            doc: True, the the dataloader will make sure that the chromosomes don't start with chr.
        label_dtype:
            doc: None, datatype of the task labels taken from the intervals_file. Allowed - string', 'int', 'float', 'bool'
        required_seq_len:
            doc: None, required sequence length.
        # max_seq_len:
        #     doc: maximum allowed sequence length
        use_strand:
            doc: reverse-complement fasta sequence if bed file defines negative strand
        force_upper:
            doc: Force uppercase output of sequences
        auto_resize:
            doc: >
                 Automatically resize the given bed input to the required_seq_len. Allowed arguments:
                 'start': keeps the start coordinate, 'end', 'center' accordingly.
    output_schema:
        inputs:
            name: seq
            shape: ()
            doc: DNA sequence as string
            special_type: DNAStringSeq
            associated_metadata: ranges
        targets:
            shape: (None,)
            doc: (optional) values following the bed-entry - chr  start  end  target1   target2 ....
        metadata:
            ranges:
                type: GenomicRanges
                doc: Ranges describing inputs.seq
    """

    def __init__(self,
                 intervals_file,
                 fasta_file,
                 num_chr_fasta=False,
                 label_dtype=None,
                 required_seq_len=None,
                 # max_seq_len=None,
                 use_strand=False,
                 force_upper=True,
                 auto_resize=None):

        self.num_chr_fasta = num_chr_fasta
        self.intervals_file = intervals_file
        self.fasta_file = fasta_file
        self.required_seq_len = required_seq_len
        self.use_strand = use_strand
        self.force_upper = force_upper
        # self.max_seq_len = max_seq_len
        self.auto_resize = auto_resize

        self.bed = BedDataset(self.intervals_file,
                              num_chr=self.num_chr_fasta,
                              label_dtype=parse_dtype(label_dtype))
        self.fasta_extractors = None

    def __len__(self):
        return len(self.bed)

    def __getitem__(self, idx):
        if self.fasta_extractors is None:
            self.fasta_extractors = FastaStringExtractor(self.fasta_file, use_strand=self.use_strand,
                                                         force_upper=self.force_upper)

        interval, labels = self.bed[idx]

        if self.required_seq_len is not None:
            if not interval.stop - interval.start == self.required_seq_len:
                if self.auto_resize is not None:
                    interval = resize_interval(interval, self.auto_resize, self.required_seq_len)
                else:
                    raise Exception("Sequence interval in intervals_file does not match required model sequence "
                                    "length. Update intervals_file or use the 'auto_resize' argument.")

        # QUESTION: @kromme - why to we need max_seq_len?
        # if self.max_seq_len is not None:
        #     assert interval.stop - interval.start <= self.max_seq_len

        # Run the fasta extractor and transform if necessary
        seq = self.fasta_extractors.extract(interval)

        return {
            "inputs": np.array(seq),
            "targets": labels,
            "metadata": {
                "ranges": GenomicRanges(interval.chrom, interval.start, interval.stop, str(idx))
            }
        }

    @classmethod
    def default_shape(cls):
        # correct the output schema - TODO - required?
        # self.output_schema_params = deepcopy(self.output_schema_params)
        # self.output_schema_params['inputs_shape'] = (1,)
        # if self.bed.n_tasks != 0:
        #     self.output_schema_params['targets_shape'] = (self.bed.n_tasks,)

        # self.output_schema = get_seq_dataset_output_schema(**self.output_schema_params)
        pass


# TODO - check lzamparo's dataloader:
# - https://github.com/kipoi/kipoiseq/issues/1#issuecomment-427412487
# - https://raw.githubusercontent.com/lzamparo/bindspace_revisions/master/deepbind/src/dataloader.py


@kipoi_dataloader(override={"dependencies": deps, 'info.authors': package_authors})
class SeqDataset(Dataset):
    """
    info:
        doc: >
            Dataloader for a combination of fasta and tab-delimited input files such as bed files. The dataloader extracts
            regions from the fasta file as defined in the tab-delimited `intervals_file` and converts them into one-hot encoded
            format. Returned sequences are of the type np.array with the shape inferred from the arguments: `alphabet_axis`
            and `dummy_axis`.
    args:
        intervals_file:
            doc: bed3+<columns> file path containing intervals + (optionally) labels
            example: example_files/intervals_files_ENCSR000EMT_chr21_10000.tsv
        fasta_file:
            doc: Reference genome FASTA file path.
            example: example_files/chr21.fa
        num_chr_fasta:
            doc: True, the the dataloader will make sure that the chromosomes don't start with chr.
        label_dtype:
            doc: None, datatype of the task labels taken from the intervals_file. Allowed - string', 'int', 'float', 'bool'
        required_seq_len:
            doc: None, required sequence length.
        use_strand:
            doc: reverse-complement fasta sequence if bed file defines negative strand
        auto_resize:
            doc: >
                 Automatically resize the given bed input to the required_seq_len. Allowed arguments:
                 'start': keeps the start coordinate, 'end', 'center' accordingly.
        alphabet_axis:
            doc: axis along which the alphabet runs (e.g. A,C,G,T for DNA)
        dummy_axis:
            doc: defines in which dimension a dummy axis should be added. None if no dummy axis is required.
        alphabet:
            doc: >
                alphabet to use for the one-hot encoding. This defines the order of the one-hot encoding.
                Can either be a list or a string: 'DNA', 'RNA', 'AMINO_ACIDS'.
    output_schema:
        inputs:
            name: seq
            shape: (None, 4)
            doc: One-hot encoded DNA sequence
            special_type: DNASeq
            associated_metadata: ranges
        targets:
            shape: (None,)
            doc: (optional) values following the bed-entry - chr  start  end  target1   target2 ....
        metadata:
            ranges:
                type: GenomicRanges
                doc: Ranges describing inputs.seq
    """

    def __init__(self,
                 intervals_file,
                 fasta_file,
                 num_chr_fasta=False,
                 label_dtype=None,
                 required_seq_len=None,
                 # max_seq_len=None,
                 use_strand=False,
                 auto_resize=None,
                 alphabet_axis=1,
                 dummy_axis=None,
                 alphabet="ACGT"):
        # transform parameters
        self.alphabet_axis = alphabet_axis
        self.dummy_axis = dummy_axis
        self.alphabet = parse_alphabet(alphabet)

        # core dataset
        self.seq_string_dataset = SeqStringDataset(intervals_file, fasta_file, num_chr_fasta=num_chr_fasta,
                                                   label_dtype=label_dtype, required_seq_len=required_seq_len,
                                                   # max_seq_len=max_seq_len,
                                                   auto_resize=auto_resize,
                                                   use_strand=use_strand, force_upper=True)

        # how to transform the input
        self.input_tranform = Compose([
            OneHot(self.alphabet),  # one-hot-encode
            DummyAxis(self.dummy_axis),  # optionally inject the dummy axis
            SwapAxes(1, self.alphabet_axis),  # put the alphabet axis elsewhere
        ])

    def __len__(self):
        return len(self.seq_string_dataset)

    def __getitem__(self, idx):
        ret = self.seq_string_dataset[idx]
        ret['inputs'] = self.input_tranform(ret["inputs"])
        return ret

    # TODO - compute the output shape based on the default value of parameters
    #         - executed in kipoi_dataloader
    # TODO - how to specify the shape properly when using differnet default parameters?
    #         - example: Basset dataloader
    @classmethod
    def default_shape(cls):
        # setup output schema
        # self.output_schema_params = deepcopy(self.output_schema_params)

        # self.output_schema_params['inputs_shape'] = get_onehot_shape(self.alphabet_axis, self.dummy_axis,
        #                                                              self.required_seq_len, self.alphabet)
        # if self.bed.n_tasks != 0:
        #     self.output_schema_params['targets_shape'] = (self.bed.n_tasks,)
        # self.output_schema = get_seq_dataset_output_schema(**self.output_schema_params)
        pass
