"""Functions for corpus preprocessing."""

import os
import sys

from nmtwizard.logger import get_logger
from sampler import sample
import loader
import prepoperator
import consumer
import tokenizer

logger = get_logger(__name__)

def _generate_models(config, tok_config, corpus_dir, data_dir, option):

    build_option = "build_" + option

    opt_multi = tok_config.get('multi', {}).get(build_option)
    opt_source = tok_config.get('source', {}).get(build_option)
    opt_target = tok_config.get('target', {}).get(build_option)

    if not opt_multi and not (opt_source and opt_target):
        logger.warning('No \'%s\' option specified, exit without preprocessing.' % build_option)
        return

    if (opt_multi and opt_source) or \
       (opt_multi and opt_target) :
        raise RuntimeError('Cannot specify \'%s\' for both \'multi\' and either \'source\' or \'target\'.' % build_option)

    # Generate preprocessed sentences and feed them to subword learners or to vocabularies.
    generate_preprocessed_data(config, corpus_dir, data_dir, option)

def get_final_tok_config(config):
    tok_config = None
    if "preprocess" in config:
        for op in reversed(config["preprocess"]):
            if not "op" in op:
                raise RuntimeError('Every step in \'preprocess\' must have a mandatory \'op\' option.')
            # TODO : as is, we always use the last tokenization for buildvocab
            # Should that be an option ?
            if op["op"] == "tokenization":
                tok_config = op
                break

    return tok_config


def generate_vocabularies(config, corpus_dir, data_dir):

    tok_config = get_final_tok_config(config)

    if not tok_config:
        raise RuntimeError('No \'tokenization\' operator in preprocess configuration, cannot build vocabularies.)')

    if ('source' not in tok_config or 'target' not in tok_config) and 'multi' not in tok_config :
        raise RuntimeError('Final \'tokenization\' operator should contain \
                            either both \'source\' and \'target\' fields \
                            or \'multi\' field.')

    for side in tok_config:
        if side == "op":
            continue
        if tok_config[side].get('vocabulary', {}):
            raise RuntimeError('Cannot build vocabulary if \'%s\' vocabulary path is already specified.' % side)
        build_vocab = tok_config[side].get('build_vocabulary')
        if build_vocab and not build_vocab.get('size'):
            raise RuntimeError('\'size\' option is mandatory to build vocabulary for \'%s\'.' % side)

    _generate_models(config, tok_config, corpus_dir, data_dir, 'subword')

    _generate_models(config, tok_config, corpus_dir, data_dir, 'vocabulary')

    preprocess_config = None
    if "preprocess" in config:
        preprocess_config = config["preprocess"]

    # TODO V2 : we don't need to return tokenization ?
    return {}, preprocess_config


def generate_preprocessed_data(config, corpus_dir, data_dir, result='preprocess'):

    # TODO V2 : annotations
    # TODO V2 : file-specific rules/extra

    # For backward compatibility with old relative path configurations.
    train_dir = 'train'
    if 'data' in config :
        if 'train_dir' in config['data']:
            train_dir = config['data']['train_dir']
    else :
        logger.warning('No \'data\' field in configuration, \
                        default corpus directory and all corpora are used.)')

    # Default data path.
    data_path = os.path.join(corpus_dir, train_dir)

    num_samples = None
    summary = None
    metadata = None

    # If some sampling OR preprocessing is applied, change result directory.
    if 'data' in config or 'preprocess' in config:

        result_dir = os.path.join(data_dir, result)
        if not os.path.exists(result_dir):
            os.mkdir(result_dir)
        if not os.path.isdir(result_dir):
            raise RuntimeError('%s is not a directory' % result_dir)
        logger.info('Generating data to %s', result_dir)

        # Sample files and write information to a special file structure.
        all_files, summary, metadata = sample(config, data_path)

        num_samples = 0

        # Default batch size is the whole sample size.
        # batch_size = f.lines_kept
        # TODO : check if we need itb
        batch_size = sys.maxsize
        if 'preprocess' in config and 'batch_size' in config['preprocess'] :
            batch_size = config['preprocess']['batch_size']

        sampler_consumer = consumer.make_consumer(config, result_dir, result)
        preprocessor = Processor(loader.SamplerFileLoader(batch_size),
                                 prepoperator.Pipeline(config),
                                 sampler_consumer)
        for f in all_files:
            lines_filtered = 0
            if f.lines_kept :
                preprocessor.open_files(f)
                _, lines_filtered = preprocessor.process()
                preprocessor.close_files()

            sampler_consumer.finalize(config)

            if lines_filtered != f.lines_kept:
                num_samples += lines_filtered
                summary[f.base_name]["lines_filtered"] = lines_filtered
            else:
                num_samples += f.lines_kept
                summary[f.base_name]["lines_filtered"] = f.lines_kept

        data_path = result_dir

    return data_path, train_dir, num_samples, summary, metadata

def preprocess_file(config, input):

    preprocessor = FileProcessor(config)

    output = "%s.tok" % input

    preprocessor.open_files([[input], output])
    preprocessor.process()
    preprocessor.close_files()

    return output, preprocessor

def postprocess_file(config, processor, source, target):

    if not processor :
        processor = FileProcessor(config)

    processor.build_postprocess_pipeline(config)

    # TODO :  can this file be compressed ?
    output = "%s.detok" % target

    # TODO deal with tokenized/detokenized
    processor.open_files([[source, target], output])
    processor.process()
    processor.close_files()

    return output


class Processor(object):

    def __init__(self, loader, pipeline, consumer):
        self._loader = loader
        self._pipeline = pipeline
        self._consumer = consumer
        self._postprocess = False

        self._src_tokenizer = pipeline.state["src_tokenizer"]
        self._tgt_tokenizer = pipeline.state["tgt_tokenizer"]

    def build_postprocess_pipeline(self, config):
        # Inverse preprocess pipeline and add postprocess pipeline
        self._pipeline.build_postprocess_pipeline(config)
        self._postprocess = True

    def open_files(self, files):
        if isinstance(files, list):
            input_files = files[0]
            output_files = files[1]
        else:
            # Input and output information can be in the same file structure (ex. SamplerFile).
            input_files = files
            output_files = files
        self._loader.open_files(input_files)
        self._consumer.open_files(output_files)

    def close_files(self):
        self._loader.close_files()
        self._consumer.close_files()

    def process(self, input=None):

        loader_args = {
            "postprocess": self._postprocess
        }
        if input:
            # Without input, loader loads data from files.
            loader_args["input"] = input
        if self._postprocess:
            # If we are in postprocess, loader should be aware of the current tokenization.
            loader_args["src_tokenizer"] = self._src_tokenizer
            loader_args["tgt_tokenizer"] = self._tgt_tokenizer

        consumer_args = {}
        if self._postprocess:
            # If we are in postprocess, consumer should be aware to output detokenized text.
            consumer_args["postprocess"] = self._postprocess

        output = []
        lines_num = 0

        # TODO V2 : parallelization
        for tu_batch in self._loader(**loader_args):
            tu_batch = self._pipeline(tu_batch)
            res_batch = self._consumer(tu_batch, **consumer_args)
            lines_num += len(tu_batch)
            # if consumer returns something, add it to output
            if res_batch :
                output.extend(res_batch)

        return output, lines_num


class FileProcessor(Processor):

    def __init__(self, config):
        super(FileProcessor, self).__init__(loader.FileLoader(),
                                            prepoperator.Pipeline(config),
                                            consumer.FileWriter())
class BasicProcessor(Processor):

    def __init__(self, config):
         super(BasicProcessor, self).__init__(loader.BasicLoader(),
                                              prepoperator.Pipeline(config),
                                              consumer.BasicWriter())

    def open_files(self, *args):
        raise NotImplementedError()

    def close_files(self, *args):
        raise NotImplementedError()
