import string
import hashlib
#import luigi.contrib.hdfs
#import luigi.contrib.hadoop_jar
import shutil
from common import *
from crawl_job_tasks import CheckJobStopped
import os


def get_disk_path(path):
    # Prefix the original path with the HDFS root folder, stripping any leading '/' so the path is considered relative
    return os.path.join(h3().hdfs_root_folder, path.lstrip(os.path.sep))


# The return is wrong and needs to be fixed
def get_date_prefix(path):
    return os.path.join(h3().hdfs_root_folder, path.lstrip(os.path.sep))


class UploadFileToDisk(luigi.Task):
    """
    This copies files to external location
    """
    task_namespace = 'file'
    path = luigi.Parameter()
    # I'm not shure, is this needed?
    resources = '?'

    def output(self):
        t = get_disk_path(self.path)
        logger.info("Output is %s" % t.path)
        return t

    def run(self):
        """
        The local file is the self.path
        The remote file is self.output().path

        :return: none
        """
        self.uploader(self.path, self.output().path)

        # Will just rewrite this method to copy file to another location on disk
    @staticmethod
    def uploader(source_path, destination_path):
        """
        Copy file
        """
        tmp_path = "%s.temp" % destination_path
        shutil.copy(source_path, tmp_path)

        # Check if destination file exists and raise an exception if so:
        if os.path.isfile(destination_path):
            raise Exception("Path %s already exists! This should never happen!" % destination_path)

        # Move file into the right place
        shutil.move(tmp_path, destination_path)

        # Log successful copy:
        logger.info("Upload completed for %s" % destination_path)


# Needs some more work
class ForceUploadToDisk(luigi.Task):
    # Same as upload file, but will overwrite file

    def complete(self):
        return False

    def output(self):
        return None

    # This might work, somehow....
    def run(self):
        # copy file to dir
        logger.info("Local hash, pre: %s" % hashlib.md5(self.output().path))
        with open(self.path, 'r') as f:
            #data_to_write_data = f.read()
            f.write(f, self.output().path())
            # client.client.write(data=f, hdfs_path=self.output().path, overwrite=True)
            # Fix this
        f.close()
        logger.info("File hash, post:  %s" % hashlib.md5(self.output().path))


class CloseOpenWarcFile(luigi.Task):
    """
    Close files that has been left .open
    """

    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Paramater()
    path = luigi.Parameter()

    # Requires that the job is stopped:
    def requires(self):
        return CheckJobStopped(self.job, self.launch_id)

    def output(self):
        return luigi.LocalTarget(self.path)

    def run(self):
        open_path = "%s.open" % self.path
        if os.path.isfile(open_path) and not os.path.isfile(self.path):
            logger.erinfo("Found an open file that needs closing: %s" % open_path)
            shutil.move(open_path, self.path)


class ClosedWarcFile(luigi.ExternalTask):
    """
    An external process is responsible to closing open WARC files, so we declare it here
    """
    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Parameter()
    path = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.path)


class CalculateLocalHash(luigi.Task):
    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Paramter()
    path = luigi.Parameter()
    force_close = luigi.BoolParameter(default=False)

    # If instructed, force the closure of any open WARC files.

    def requires(self):
        if self.force_close:
            return CloseOpenWarcFile(self.job, self.launch_id, self.path)
        else:
            return ClosedWarcFile(self.job, self.launch_id, self.path)

    def output(self):
        return hash_target(self.job, self.launch_id, "%s.local.sha512" % self.path)

    def run(self):
        logger.debug("file %s to hash" % (self.path))

        t = luigi.LocalTarget(self.path)
        with t.open('r') as reader:
            file_hash = hashlib.sha512(reader.read()).hexdigest()

        # Test hash
        CalculateLocalHash.check_hash(self.path, file_hash)

        with self.output().open('w') as f:
            f.write(file_hash)

    @staticmethod
    def check_hash(path, file_hash):
        logger.debug("Checking file %s hash %s" % (path, file_hash))
        if len(file_hash) != 128:
            raise Exception("%s hash not 128 character length [%s]" % (path, len(file_hash)))
        if not all(c in string.hexdigits for c in file_hash):
            raise Exception("%s hash not all hex [%s]" % (path, file_hash))


class CalculateHash(luigi.Task):
    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Parameter()
    path = luigi.Parameter
    resources = {}

    def requires(self):
        return UploadFileToDisk(self.path)

    def output(self):
        return hash_target(self.job, self.launch_id, "%s.disk.sha512" % self.path)

    def run(self):
        logger.debug("file %s to hash" % (self.path))

        # get hash for local or copied file
        t = self.input()

        with t.open('r') as reader:
            file_hash = hashlib.sha512(reader.read()).hexdigest()

        # test hash
        CalculateLocalHash.check_hash(self.path, file_hash)

        with self.output().open('w') as f:
            f.write(file_hash)


class MoveToDisk(luigi.task):
    task_namespace = 'file'
    job = luigi.EnumParamter(enum=Jobs)
    launch_id = luigi.Parameter()
    path = luigi.Parameter()
    delete_local = luigi.BoolParameter(default=False)

    def requires(self):
        return [CalculateLocalHash(self.job, self.launch_id, self.path),
                CalculateHash(self.job, self.launch_id, self.path)]

    def output(self):
        return hash_target(self.job, self.launch_id, "%s.transferred" % self.path)

    def run(self):
        #Read in sha512
        with self.input()[0].open('r') as f:
            local_hash = f.readline()
        logger.info("Got local hash %s" % local_hash)
        # Re-Download and get the hash
        with self.input()[1].open('r') as f:
            external_hash = f.readline()
        logger.info("Got external hash %s" % external_hash)

        if local_hash != external_hash:
            raise Exception("Local & external hashes do not match for %s" % self.path)

        # Otherwise, mbe to external was good, so delete:
        if self.delete_local:
            os.remove(str(self.path))
        #And write out success
        with self.output().open('w') as f:
            f.write(external_hash)


class MoveToExternalHddIfStopped(luigi.Task):
    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Parameter()
    path = luigi.Parameter()
    delete_local = luigi.BoolParameter(default=False)

    # Require that the job is stopped:
    def requires(self):
        return CheckJobStopped(self.job, self.launch_id)

    # Use the output of the underlying MoveToExternalHdd call:
    def output(self):
        return MoveToDisk(self.job, self.launch_id, self.path, self.delete_local).output()

    # Call the MoveToExternalHdd task as a dynamic dependency:
    def run(self):
        yield MoveToDisk(self.job, self.launch_id, self.path, self.delete_local)


class MoveToWarcsFolder(luigi.Task):
    """
    This can be used to move a WARC that's outside the
    """
    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Parameter()
    path = luigi.Parameter()

    # Requires the source path to be presented and closed:
    def requires(self):
        return ClosedWarcFile(self.job, self.launch_id, self.path)

    # Specify the target folder:
    def output(self):
        return luigi.LocalTarget("%s/output/warcs/%s/%s/%s" % (h3().local_root_folder, self.job.name, self.launch_id, os.path.basename(self.path)))

    # when run, must move the file:
    def run(self):
        shutil.move(self.path, self.output().path)


class MoveFilesForLaunch(luigi.Task):
    """
    Move all the files associated with one launch
    """
    task_namespace = 'file'
    job = luigi.EnumParameter(enum=Jobs)
    launch_id = luigi.Parameter()
    delete_local = luigi.BoolParameter(default=False)

    def output(self):
        return otarget(self.job, self.launch_id, "all-moved")

    def run(self):
        logger.info("Looking in %s %s" % (self.job, self.launch_id))
        # Look in /heritrix/output/wren files and move them to the /warcs/ folder:
        for wren_item in glob.glob("%s/*-%s-%s-*.warc.gz" % (h3().local_wren_folder, self. job.name, self.launch_id)):
            yield MoveToWarcsFolder(self.job, self.launch_id, wren_item)
        # Look in warcs and viral for WARCs e.g in /heritrix/output/{warcs|viral}/{job.name}/{launch_id}
        for out_type in ['warcs', 'viral']:
            glob_path = "%s/output/%s/%s/%s/*.warc.gz" % (h3().local_root_folder, out_type, self.job.name, self.launch_id)
            logger.info("GLOB:%s" % glob_path)
            for item in glob.glob("%s/output/%s/%s/%s/*.warc.gz" % (h3().local_root_folder, out_type, self.job.name, self.launch_id)):
                logger.info("ITEM:%s" % item)
                yield MoveToDisk(self.job, self.launch_id, item, self.delete_local)
        # And look for /heritrix/output/logs:
        for log_item in glob.glob("%s/output/logs/%s/%s/*.log*" % (h3().local_root_folder, self.job.name, self.launch_id)):
            if os.path.splitext(log_item)[1] == '.lck':
                continue
            elif os.path.splitext(log_item)[1] == '.log':
                # Only move files with the '.log' suffix if this job is no-longer running:
                logger.info("Using MoveToHdfsIfStopped for %s" % log_item)
                yield MoveToExternalHddIfStopped(self.job, self.launch_id, log_item, self.delete_local)
            else:
                yield MoveToDisk(self.job, self.launch_id, log_item, self.delete_local)

        # and write out success
        with self.output().open('w') as f:
            f.write("MOVED")


class ScanForFilesToMove(ScanForLaunches):
    """
    This scans for files associated with a particular launch of a given job and starts MoveToHdfs for each,
    """
    delete_local = luigi.BoolParameter(default=False)

    task_namespace = 'scan'
    scan_name = 'move-to-hdfs'

    def scan_job_launch(self, job, launch):
        logger.info("Looking at moving files for %s %s" % (job, launch))
        yield MoveFilesForLaunch(job, launch, self.delete_local)


if __name__ == '__main__':
    luigi.run(['scan.ScanForFilesToMove', '--date-interval', '2016-11-01-2016-11-10'])
