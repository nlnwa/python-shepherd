import os
import io
import sys
import logging
import luigi
import luigi.contrib.hdfs
import luigi.contrib.hadoop
import urlparse
import hanzo, hanzo.warctools, hanzo.warcpayload
from hanzo.warctools import WarcRecord
try:
    from http.client import HTTPResponse
except ImportError:
    from httplib import HTTPResponse

logger = logging.getLogger('luigi-interface')

#
# This was an experiment with Python-based streaming Hadoop jobs for performing basic processing of warcs
# and e.g. generating stats. However all implementations (`warctools`, `warc` and `pywb`) require
# behaviour that is 'difficult' to support. For the first two, both use Python's gzip support,
# which requires seekable streams (e.g. `seek(offset,whence)` support). Python Wayback (`pywb`)
# does not appear to depend on that module, but required `ffi` support for native calls, which
# makes deployment more difficult. Therefore, we use Java map-reduce jobs for WARC parsing, but
# we can generate simple line-oriented text files from the WARCs, after which streaming works
# just fine.
#
# Also attempted to use Hadoop's built-in auto-gunzipping support, which is built into streaming mode.
# After some difficulties, this could be made to work, but was unreliable as different nodes would behave differently
# with respect to keeping-going when gunzipping concateneted gz records.
#

class ExternalListFile(luigi.ExternalTask):
    """
    This ExternalTask defines the Target at the top of the task chain. i.e. resources that are overall inputs rather
    than generated by the tasks themselves.
    """
    input_file = luigi.Parameter()

    def output(self):
        """
        Returns the target output for this task.
        In this case, it expects a file to be present in HDFS.
        :return: the target output for this task.
        :rtype: object (:py:class:`luigi.target.Target`)
        """
        return luigi.contrib.hdfs.HdfsTarget(self.input_file)


class GenerateWarcStatsIndirect(luigi.contrib.hadoop.JobTask):
    """
    Generates the WARC stats by reading each file in turn. Data is therefore no-local.

    Parameters:
        input_file: The file (on HDFS) that contains the list of WARC files to process
    """
    input_file = luigi.Parameter()

    def output(self):
        out_name = "%s-stats.tsv" % os.path.splitext(self.input_file)[0]
        return luigi.contrib.hdfs.HdfsTarget(out_name, format=luigi.contrib.hdfs.PlainDir)

    def requires(self):
        return ExternalListFile(self.input_file)

    def extra_modules(self):
        return []

    def extra_files(self):
        return ["luigi.cfg"]

    def mapper(self, line):
        """
        Each line should be a path to a WARC file on HDFS

        We open each one in turn, and scan the contents.

        The pywb record parser gives access to the following properties (at least):

        entry['urlkey']
        entry['timestamp']
        entry['url']
        entry['mime']
        entry['status']
        entry['digest']
        entry['length']
        entry['offset']

        :param line:
        :return:
        """

        # Ignore blank lines:
        if line == '':
            return

        warc = luigi.contrib.hdfs.HdfsTarget(line)
        #entry_iter = DefaultRecordParser(sort=False,
        #                                 surt_ordered=True,
       ##                                  include_all=False,
        #                                 verify_http=False,
        #                                 cdx09=False,
        #                                 cdxj=False,
        #                                 minimal=False)(warc.open('rb'))

        #for entry in entry_iter:
        #    hostname = urlparse.urlparse(entry['url']).hostname
        #    yield hostname, entry['status']

    def reducer(self, key, values):
        """

        :param key:
        :param values:
        :return:
        """
        for value in values:
            yield key, sum(values)


class ExternalFilesFromList(luigi.ExternalTask):
    """
    This ExternalTask defines the Target at the top of the task chain. i.e. resources that are overall inputs rather
    than generated by the tasks themselves.
    """
    input_file = luigi.Parameter()

    def output(self):
        """
        Returns the target output for this task.
        In this case, it expects a file to be present in HDFS.
        :return: the target output for this task.
        :rtype: object (:py:class:`luigi.target.Target`)
        """
        for line in open(self.input_file, 'r').readlines():
            yield luigi.contrib.hdfs.HdfsTarget(line.strip(), format=luigi.contrib.hdfs.format.PlainFormat)


class GenerateWarcStats(luigi.contrib.hadoop.JobTask):
    """
    Generates the Warc stats by reading in each file and splitting the stream into entries.
    As this uses the stream directly and so data-locality is preserved.

    Parameters:
        input_file: The file (on HDFS) that contains the list of WARC files to process
    """
    input_file = luigi.Parameter()

    def output(self):
        out_name = "%s-stats.tsv" % os.path.splitext(self.input_file)[0]
        return luigi.contrib.hdfs.HdfsTarget(out_name, format=luigi.contrib.hdfs.PlainDir)

    def requires(self):
        return ExternalFilesFromList(self.input_file)

    def extra_files(self):
        return ["luigi.cfg"]

    def extra_modules(self):
        return [hanzo]

    def job_runner(self):
        class BinaryInputHadoopJobRunner(luigi.contrib.hadoop.HadoopJobRunner):
            """
            A job runner to use the UnsplittableInputFileFormat (based on DefaultHadoopJobRunner):
            """
            def __init__(self):
                config = luigi.configuration.get_config()
                streaming_jar = config.get('hadoop', 'streaming-jar')
                super(BinaryInputHadoopJobRunner, self).__init__(
                    streaming_jar=streaming_jar,
                    input_format="uk.bl.wa.hadoop.mapred.UnsplittableInputFileFormat",
                    libjars=["../jars/warc-hadoop-recordreaders-2.2.0-BETA-7-SNAPSHOT-job.jar"])

        return BinaryInputHadoopJobRunner()

    def run_mapper(self, stdin=sys.stdin, stdout=sys.stdout):
        """
        Run the mapper on the hadoop node.
        ANJ: Creating modified version to pass through the raw stdin
        """
        self.init_hadoop()
        self.init_mapper()
        outputs = self._map_input(stdin)
        if self.reducer == NotImplemented:
            self.writer(outputs, stdout)
        else:
            self.internal_writer(outputs, stdout)

    def reader(self, stdin):
        # Special reader to read the input stream and yield WARC records:
        class TellingReader():

            def __init__(self, stream):
                self.stream = io.open(stream.fileno(),'rb')
                self.pos = 0

            def read(self, size=None):
                logger.warning("read()ing from current position: %i" % self.pos)
                chunk = self.stream.read(size)
                logger.warning("read() %s" % chunk)
                self.pos += len(chunk)
                logger.warning("read()ing current position now: %i" % self.pos)
                return chunk

            def readline(self,size=None):
                logger.warning("readline()ing from current position: %i" % self.pos)
                line = self.stream.readline(size)
                logger.warning("readline() %s" % line)
                self.pos += len(bytes(line))
                logger.warning("readline()ing current position now: %i" % self.pos)
                return line

            def tell(self):
                logger.warning("tell()ing current position: %i" % self.pos)
                return self.pos

        fh = hanzo.warctools.WarcRecord.open_archive(filename="dummy.warc",
                                                     file_handle=TellingReader(stdin),
                                                     gzip=None)

        for (offset, record, errors) in fh.read_records(limit=None):
            logger.warning("GOT %s :: %s :: %s" % (offset, record, errors))
            if record:
                logger.warning("WarcRecord type=%s" % record.type)
                yield record,
            else:
                logging.error(errors)

    def mapper(self, record):
        """

        :param record:
        :return:
        """

        # Look at HTTP Responses:
        if (record.type == WarcRecord.RESPONSE
                and record.content_type.startswith(b'application/http')):
            # Parse the HTTP Headers, faking a socket wrapper:
            f = hanzo.warcpayload.FileHTTPResponse(record.content_file)
            f.begin()

            hostname = urlparse.urlparse(record.url).hostname
            yield hostname, f.status

    def reducer(self, key, values):
        """

        :param key:
        :param values:
        :return:
        """
        for value in values:
            yield key, sum(values)




if __name__ == '__main__':
    luigi.run(['GenerateWarcStats', '--input-file', 'daily-warcs-test.txt', '--local-scheduler'])
