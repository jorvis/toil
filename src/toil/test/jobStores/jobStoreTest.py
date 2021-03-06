# Copyright (C) 2015-2016 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division

from future import standard_library
standard_library.install_aliases()
from builtins import next
from builtins import str
from builtins import range
from past.utils import old_div
from builtins import object
import socketserver
import pytest
import hashlib
import logging
import threading
import os
import shutil
import tempfile
import time
import uuid
from stubserver import FTPStubServer
from abc import abstractmethod, ABCMeta
from itertools import chain, islice, count
from threading import Thread
from unittest import skip

# Python 3 compatibility imports
from six.moves.queue import Queue
from six.moves import xrange, socketserver as SocketServer, SimpleHTTPServer
from six import iteritems
import six.moves.urllib.parse as urlparse
from six.moves.urllib.request import urlopen, Request

from bd2k.util import memoize
from bd2k.util.exceptions import panic
# noinspection PyPackageRequirements
# (installed by `make prepare`)
from mock import patch

from toil.common import Config, Toil
from toil.job import Job, JobNode
from toil.jobStores.abstractJobStore import (AbstractJobStore,
                                             NoSuchJobException,
                                             NoSuchFileException)
from toil.jobStores.aws.utils import region_to_bucket_location
from toil.jobStores.fileJobStore import FileJobStore
from toil.test import (ToilTest,
                       needs_aws,
                       needs_azure,
                       needs_encryption,
                       make_tests,
                       needs_google,
                       slow,
                       experimental)
from future.utils import with_metaclass

logger = logging.getLogger(__name__)


def tearDownModule():
    AbstractJobStoreTest.Test.cleanUpExternalStores()


class AbstractJobStoreTest(object):
    """
    Hide abstract base class from unittest's test case loader

    http://stackoverflow.com/questions/1323455/python-unit-test-with-base-and-sub-class#answer-25695512
    """

    class Test(with_metaclass(ABCMeta, ToilTest)):
        @classmethod
        def setUpClass(cls):
            super(AbstractJobStoreTest.Test, cls).setUpClass()
            logging.basicConfig(level=logging.DEBUG)
            logging.getLogger('boto').setLevel(logging.CRITICAL)

        # The use of @memoize ensures that we only have one instance of per class even with the
        # generative import/export tests attempts to instantiate more. This in turn enables us to
        # share the external stores (buckets, blob store containers, local directory, etc.) used
        # for testing import export. While the constructor arguments are included in the
        # memoization key, I have only ever seen one case: ('test', ). The worst that can happen
        # if other values are also used is that there will be more external stores and less sharing
        # of them. They will still all be cleaned-up.

        @classmethod
        @memoize
        def __new__(cls, *args):
            return super(AbstractJobStoreTest.Test, cls).__new__(*args)

        def _createConfig(self):
            return Config()

        @abstractmethod
        def _createJobStore(self):
            """
            :rtype: AbstractJobStore
            """
            raise NotImplementedError()

        def setUp(self):
            super(AbstractJobStoreTest.Test, self).setUp()
            self.namePrefix = 'jobstore-test-' + str(uuid.uuid4())
            self.master = self._createJobStore()
            self.config = self._createConfig()
            self.master.initialize(self.config)
            self.arbitraryRequirements = {'memory': 1, 'disk': 2, 'cores': 1, 'preemptable': False}
            self.arbitraryJob = JobNode(command='command',
                                        jobStoreID=None,
                                        jobName='arbitrary', unitName=None,
                                        requirements=self.arbitraryRequirements)

        def tearDown(self):
            self.master.destroy()
            super(AbstractJobStoreTest.Test, self).tearDown()

        def test(self):
            """
            This is a front-to-back test of the "happy" path in a job store, i.e. covering things
            that occur in the dat to day life of a job store. The purist might insist that this be
            split up into several cases and I agree wholeheartedly.
            """
            master = self.master

            # Test initial state
            #
            self.assertFalse(master.exists('foo'))
            self.assertRaises(NoSuchJobException, master.load, 'foo')

            # Create parent job and verify its existence/properties
            #
            masterRequirements = dict(memory=12, cores=34, disk=35, preemptable=True)
            jobNodeOnMaster = JobNode(command='master1',
                                      requirements=masterRequirements,
                                      jobName='test1', unitName='onMaster',
                                      jobStoreID=None, predecessorNumber=0)
            jobOnMaster = master.create(jobNodeOnMaster)
            self.assertTrue(master.exists(jobOnMaster.jobStoreID))
            self.assertEquals(jobOnMaster.command, 'master1')
            self.assertEquals(jobOnMaster.memory, masterRequirements['memory'])
            self.assertEquals(jobOnMaster.cores, masterRequirements['cores'])
            self.assertEquals(jobOnMaster.disk, masterRequirements['disk'])
            self.assertEquals(jobOnMaster.preemptable, masterRequirements['preemptable'])
            self.assertEquals(jobOnMaster.jobName, 'test1')
            self.assertEquals(jobOnMaster.unitName, 'onMaster')
            self.assertEquals(jobOnMaster.stack, [])
            self.assertEquals(jobOnMaster.predecessorNumber, 0)
            self.assertEquals(jobOnMaster.predecessorsFinished, set())
            self.assertEquals(jobOnMaster.logJobStoreFileID, None)

            


            # Create a second instance of the job store, simulating a worker ...
            #
            worker = self._createJobStore()
            worker.resume()
            self.assertEquals(worker.config, self.config)
            self.assertIsNot(worker.config, self.config)
            # ... and load the parent job there.
            jobOnWorker = worker.load(jobOnMaster.jobStoreID)
            self.assertEquals(jobOnMaster, jobOnWorker)

            # Update state on job
            #
            # The following demonstrates the job update pattern, where files to be deleted are
            # referenced in "filesToDelete" array, which is persisted to disk first. If things go
            # wrong during the update, this list of files to delete is used to remove the
            # unneeded files
            jobOnWorker.filesToDelete = ['1', '2']
            worker.update(jobOnWorker)
            # Check jobs to delete persisted
            self.assertEquals(master.load(jobOnWorker.jobStoreID).filesToDelete, ['1', '2'])
            # Create children
            childRequirements1 = dict(memory=23, cores=45, disk=46, preemptable=True)
            jobNodeOnChild1 = JobNode(command='child1',
                                      requirements=childRequirements1,
                                      jobName='test2', unitName='onChild1',
                                      jobStoreID=None)
            childRequirements2 = dict(memory=34, cores=56, disk=57, preemptable=False)
            jobNodeOnChild2 = JobNode(command='master1',
                                      requirements=childRequirements2,
                                      jobName='test3', unitName='onChild2',
                                      jobStoreID=None)
            child1 = worker.create(jobNodeOnChild1)
            child2 = worker.create(jobNodeOnChild2)
            # Update parent
            jobOnWorker.stack.append((child1, child2))
            jobOnWorker.filesToDelete = []
            worker.update(jobOnWorker)

            # Check equivalence between master and worker
            #
            self.assertNotEquals(jobOnWorker, jobOnMaster)
            # Reload parent job on master
            jobOnMaster = master.load(jobOnMaster.jobStoreID)
            self.assertEquals(jobOnWorker, jobOnMaster)
            # Load children on master an check equivalence
            self.assertEquals(master.load(child1.jobStoreID), child1)
            self.assertEquals(master.load(child2.jobStoreID), child2)

            # Test changing and persisting job state across multiple jobs
            #
            childJobs = [worker.load(childNode.jobStoreID) for childNode in jobOnMaster.stack[-1]]
            for childJob in childJobs:
                childJob.logJobStoreFileID = str(uuid.uuid4())
                childJob.remainingRetryCount = 66
                self.assertNotEquals(childJob, master.load(childJob.jobStoreID))
            for childJob in childJobs:
                worker.update(childJob)
            for childJob in childJobs:
                self.assertEquals(master.load(childJob.jobStoreID), childJob)
                self.assertEquals(worker.load(childJob.jobStoreID), childJob)

            # Test job iterator - the results of the iterator are effected by eventual
            # consistency. We cannot guarantee all jobs will appear but we can assert that all
            # jobs that show up are a subset of all existing jobs. If we had deleted jobs before
            # this we would have to worry about ghost jobs appearing and this assertion would not
            # be valid
            self.assertTrue(set(childJobs + [jobOnMaster]) >= set(worker.jobs()))
            self.assertTrue(set(childJobs + [jobOnMaster]) >= set(master.jobs()))

            # Test job deletions
            #
            # First delete parent, this should have no effect on the children
            self.assertTrue(master.exists(jobOnMaster.jobStoreID))
            self.assertTrue(worker.exists(jobOnMaster.jobStoreID))
            master.delete(jobOnMaster.jobStoreID)
            self.assertFalse(master.exists(jobOnMaster.jobStoreID))
            self.assertFalse(worker.exists(jobOnMaster.jobStoreID))

            for childJob in childJobs:
                self.assertTrue(master.exists(childJob.jobStoreID))
                self.assertTrue(worker.exists(childJob.jobStoreID))
                master.delete(childJob.jobStoreID)
                self.assertFalse(master.exists(childJob.jobStoreID))
                self.assertFalse(worker.exists(childJob.jobStoreID))
                self.assertRaises(NoSuchJobException, worker.load, childJob.jobStoreID)
                self.assertRaises(NoSuchJobException, master.load, childJob.jobStoreID)

            try:
                with master.readSharedFileStream('missing') as _:
                    pass
                self.fail('Expecting NoSuchFileException')
            except NoSuchFileException:
                pass

            # Test shared files: Write shared file on master, ...
            #
            with master.writeSharedFileStream('foo') as f:
                f.write('bar')
            # ... read that file on worker, ...
            with worker.readSharedFileStream('foo') as f:
                self.assertEquals('bar', f.read())
            # ... and read it again on master.
            with master.readSharedFileStream('foo') as f:
                self.assertEquals('bar', f.read())

            with master.writeSharedFileStream('nonEncrypted', isProtected=False) as f:
                f.write('bar')
            self.assertUrl(master.getSharedPublicUrl('nonEncrypted'))
            self.assertRaises(NoSuchFileException, master.getSharedPublicUrl, 'missing')

            # Test per-job files: Create empty file on master, ...
            #
            # First recreate job
            jobOnMaster = master.create(jobNodeOnMaster)
            fileOne = worker.getEmptyFileStoreID(jobOnMaster.jobStoreID)
            # Check file exists
            self.assertTrue(worker.fileExists(fileOne))
            self.assertTrue(master.fileExists(fileOne))
            # ... write to the file on worker, ...
            with worker.updateFileStream(fileOne) as f:
                f.write('one')
            # ... read the file as a stream on the master, ....
            with master.readFileStream(fileOne) as f:
                self.assertEquals(f.read(), 'one')

            # ... and copy it to a temporary physical file on the master.
            fh, path = tempfile.mkstemp()
            try:
                os.close(fh)
                tmpPath = path + '.read-only'
                master.readFile(fileOne, tmpPath)
                try:
                    shutil.copyfile(tmpPath, path)
                finally:
                    os.unlink(tmpPath)
                with open(path, 'r+') as f:
                    self.assertEquals(f.read(), 'one')
                    # Write a different string to the local file ...
                    f.seek(0)
                    f.truncate(0)
                    f.write('two')
                # ... and create a second file from the local file.
                fileTwo = master.writeFile(path, jobOnMaster.jobStoreID)
                with worker.readFileStream(fileTwo) as f:
                    self.assertEquals(f.read(), 'two')
                # Now update the first file from the local file ...
                master.updateFile(fileOne, path)
                with worker.readFileStream(fileOne) as f:
                    self.assertEquals(f.read(), 'two')
            finally:
                os.unlink(path)
            # Create a third file to test the last remaining method.
            with worker.writeFileStream(jobOnMaster.jobStoreID) as (f, fileThree):
                f.write('three')
            with master.readFileStream(fileThree) as f:
                self.assertEquals(f.read(), 'three')
            # Delete a file explicitly but leave files for the implicit deletion through the parent
            worker.deleteFile(fileOne)

            # Check the file is gone
            #
            for store in worker, master:
                self.assertFalse(store.fileExists(fileOne))
                self.assertRaises(NoSuchFileException, store.readFile, fileOne, '')
                try:
                    with store.readFileStream(fileOne) as _:
                        pass
                    self.fail('Expecting NoSuchFileException')
                except NoSuchFileException:
                    pass

            # Test stats and logging
            #
            stats = None

            def callback(f2):
                stats.add(f2.read())

            stats = set()
            self.assertEquals(0, master.readStatsAndLogging(callback))
            self.assertEquals(set(), stats)
            worker.writeStatsAndLogging('1')
            self.assertEquals(1, master.readStatsAndLogging(callback))
            self.assertEquals({'1'}, stats)
            self.assertEquals(0, master.readStatsAndLogging(callback))
            worker.writeStatsAndLogging('1')
            worker.writeStatsAndLogging('2')
            stats = set()
            self.assertEquals(2, master.readStatsAndLogging(callback))
            self.assertEquals({'1', '2'}, stats)
            largeLogEntry = os.urandom(self._largeLogEntrySize())
            stats = set()
            worker.writeStatsAndLogging(largeLogEntry)
            self.assertEquals(1, master.readStatsAndLogging(callback))
            self.assertEquals({largeLogEntry}, stats)

            # test the readAll parameter
            self.assertEqual(4, master.readStatsAndLogging(callback, readAll=True))

            # Delete parent
            #
            master.delete(jobOnMaster.jobStoreID)
            self.assertFalse(master.exists(jobOnMaster.jobStoreID))
            # TODO: Who deletes the shared files?

        def testBatchCreate(self):
            master = self.master
            masterRequirements = dict(memory=12, cores=34, disk=35, preemptable=True)
            jobGraphs = []
            with master.batch():
                for i in range(100):
                    overlargeJobNodeOnMaster = JobNode(command='master-overlarge',
                                        requirements=masterRequirements,
                                        jobName='test-overlarge', unitName='onMaster',
                                        jobStoreID=None, predecessorNumber=0)
                    jobGraphs.append(master.create(overlargeJobNodeOnMaster))
            for jobGraph in jobGraphs:
                self.assertTrue(master.exists(jobGraph.jobStoreID))

        def _prepareTestFile(self, store, size=None):
            """
            Generates a URL that can be used to point at a test file in the storage mechanism
            used by the job store under test by this class. Optionaly creates a file at that URL.

            :param: store: an object referencing the store, same type as _createExternalStore's
                    return value

            :param int size: The size of the test file to be created.

            :return: the URL, or a tuple (url, md5) where md5 is the file's hexadecimal MD5 digest

            :rtype: str|(str,str)
            """
            raise NotImplementedError()

        @abstractmethod
        def _hashTestFile(self, url):
            """
            Returns hexadecimal MD5 digest of the contents of the file pointed at by the URL.
            """
            raise NotImplementedError()

        @abstractmethod
        def _createExternalStore(self):
            raise NotImplementedError()

        @abstractmethod
        def _cleanUpExternalStore(self, store):
            """
            :param: store: an object referencing the store, same type as _createExternalStore's
                    return value
            """
            raise NotImplementedError()

        externalStoreCache = {}

        def _externalStore(self):
            try:
                store = self.externalStoreCache[self]
            except KeyError:
                logger.info('Creating new external store for %s', self)
                store = self.externalStoreCache[self] = self._createExternalStore()
            else:
                logger.info('Reusing external store for %s', self)
            return store

        @classmethod
        def cleanUpExternalStores(cls):
            for test, store in iteritems(cls.externalStoreCache):
                logger.info('Cleaning up external store for %s.', test)
                test._cleanUpExternalStore(store)

        mpTestPartSize = 5 << 20

        @classmethod
        def makeImportExportTests(cls):

            testClasses = [FileJobStoreTest, AWSJobStoreTest, AzureJobStoreTest, GoogleJobStoreTest]

            activeTestClassesByName = {testCls.__name__: testCls
                                       for testCls in testClasses
                                       if not getattr(testCls, '__unittest_skip__', False)}

            def testImportExportFile(self, otherCls, size):
                """
                :param AbstractJobStoreTest.Test self: the current test case

                :param AbstractJobStoreTest.Test otherCls: the test case class for the job store
                       to import from or export to

                :param int size: the size of the file to test importing/exporting with
                """
                # Prepare test file in other job store
                self.master.partSize = cls.mpTestPartSize
                other = otherCls('test')
                store = other._externalStore()

                srcUrl, srcMd5 = other._prepareTestFile(store, size)
                # Import into job store under test
                jobStoreFileID = self.master.importFile(srcUrl)
                with self.master.readFileStream(jobStoreFileID) as f:
                    fileMD5 = hashlib.md5(f.read()).hexdigest()
                self.assertEqual(fileMD5, srcMd5)
                # Export back into other job store
                dstUrl = other._prepareTestFile(store)
                self.master.exportFile(jobStoreFileID, dstUrl)
                self.assertEqual(fileMD5, other._hashTestFile(dstUrl))

            make_tests(testImportExportFile,
                       cls,
                       otherCls=activeTestClassesByName,
                       size=dict(zero=0,
                                 one=1,
                                 oneMiB=2 ** 20,
                                 partSizeMinusOne=cls.mpTestPartSize - 1,
                                 partSize=cls.mpTestPartSize,
                                 partSizePlusOne=cls.mpTestPartSize + 1))

            def testImportSharedFile(self, otherCls):
                """
                :param AbstractJobStoreTest.Test self: the current test case

                :param AbstractJobStoreTest.Test otherCls: the test case class for the job store
                       to import from or export to
                """
                # Prepare test file in other job store
                self.master.partSize = cls.mpTestPartSize
                other = otherCls('test')
                store = other._externalStore()

                srcUrl, srcMd5 = other._prepareTestFile(store, 42)
                # Import into job store under test
                self.assertIsNone(self.master.importFile(srcUrl, sharedFileName='foo'))
                with self.master.readSharedFileStream('foo') as f:
                    fileMD5 = hashlib.md5(f.read()).hexdigest()
                self.assertEqual(fileMD5, srcMd5)

            make_tests(testImportSharedFile,
                       cls,
                       otherCls=activeTestClassesByName)

        def testImportHttpFile(self):
            http = socketserver.TCPServer(('', 0), StubHttpRequestHandler)
            try:
                httpThread = threading.Thread(target=http.serve_forever)
                httpThread.start()
                try:
                    assignedPort = http.server_address[1]
                    url = 'http://localhost:%d' % assignedPort
                    with self.master.readFileStream(self.master.importFile(url)) as readable:
                        self.assertEqual(readable.read(), StubHttpRequestHandler.fileContents)
                finally:
                    http.shutdown()
                    httpThread.join()
            finally:
                http.server_close()

        def testImportFtpFile(self):
            file = {'name':'foo', 'content':'foo bar baz qux'}
            ftp = FTPStubServer(0)
            ftp.run()
            try:
                ftp.add_file(**file)
                assignedPort = ftp.server.server_address[1]
                url = 'ftp://user1:passwd@localhost:%d/%s' % (assignedPort, file['name'])
                with self.master.readFileStream(self.master.importFile(url)) as readable:
                    self.assertEqual(readable.read(), file['content'])
            finally:
                ftp.stop()

        @slow
        def testFileDeletion(self):
            """
            Intended to cover the batch deletion of items in the AWSJobStore, but it doesn't hurt
            running it on the other job stores.
            """
            master = self.master
            n = self._batchDeletionSize()
            for numFiles in (1, n - 1, n, n + 1, 2 * n):
                job = master.create(self.arbitraryJob)
                fileIDs = [master.getEmptyFileStoreID(job.jobStoreID) for _ in range(0, numFiles)]
                master.delete(job.jobStoreID)
                for fileID in fileIDs:
                    # NB: the fooStream() methods return context managers
                    self.assertRaises(NoSuchFileException, master.readFileStream(fileID).__enter__)

        @slow
        def testMultipartUploads(self):
            """
            This test is meant to cover multi-part uploads in the AWSJobStore but it doesn't hurt
            running it against the other job stores as well.
            """
            # Should not block. On Linux, /dev/random blocks when its running low on entropy
            random_device = '/dev/urandom'
            # http://unix.stackexchange.com/questions/11946/how-big-is-the-pipe-buffer
            bufSize = 65536
            partSize = self._partSize()
            self.assertEquals(partSize % bufSize, 0)
            job = self.master.create(self.arbitraryJob)

            # Test file/stream ending on part boundary and within a part
            #
            for partsPerFile in (1, 2.33):
                checksum = hashlib.md5()
                checksumQueue = Queue(2)

                # FIXME: Having a separate thread is probably overkill here

                def checksumThreadFn():
                    while True:
                        _buf = checksumQueue.get()
                        if _buf is None:
                            break
                        checksum.update(_buf)

                # Multipart upload from stream
                #
                checksumThread = Thread(target=checksumThreadFn)
                checksumThread.start()
                try:
                    with open(random_device) as readable:
                        with self.master.writeFileStream(job.jobStoreID) as (writable, fileId):
                            for i in range(int(partSize * partsPerFile / bufSize)):
                                buf = readable.read(bufSize)
                                checksumQueue.put(buf)
                                writable.write(buf)
                finally:
                    checksumQueue.put(None)
                    checksumThread.join()
                before = checksum.hexdigest()

                # Verify
                #
                checksum = hashlib.md5()
                with self.master.readFileStream(fileId) as readable:
                    while True:
                        buf = readable.read(bufSize)
                        if not buf:
                            break
                        checksum.update(buf)
                after = checksum.hexdigest()
                self.assertEquals(before, after)

                # Multi-part upload from file
                #
                checksum = hashlib.md5()
                fh, path = tempfile.mkstemp()
                try:
                    with os.fdopen(fh, 'r+') as writable:
                        with open(random_device) as readable:
                            for i in range(int(partSize * partsPerFile / bufSize)):
                                buf = readable.read(bufSize)
                                writable.write(buf)
                                checksum.update(buf)
                    fileId = self.master.writeFile(path, job.jobStoreID)
                finally:
                    os.unlink(path)
                before = checksum.hexdigest()

                # Verify
                #
                checksum = hashlib.md5()
                with self.master.readFileStream(fileId) as readable:
                    while True:
                        buf = readable.read(bufSize)
                        if not buf:
                            break
                        checksum.update(buf)
                after = checksum.hexdigest()
                self.assertEquals(before, after)
            self.master.delete(job.jobStoreID)

        def testZeroLengthFiles(self):
            job = self.master.create(self.arbitraryJob)
            nullFile = self.master.writeFile('/dev/null', job.jobStoreID)
            with self.master.readFileStream(nullFile) as f:
                self.assertEquals(f.read(), "")
            with self.master.writeFileStream(job.jobStoreID) as (f, nullStream):
                pass
            with self.master.readFileStream(nullStream) as f:
                self.assertEquals(f.read(), "")
            self.master.delete(job.jobStoreID)

        @slow
        def testLargeFile(self):
            dirPath = self._createTempDir()
            filePath = os.path.join(dirPath, 'large')
            hashIn = hashlib.md5()
            with open(filePath, 'w') as f:
                for i in range(0, 10):
                    buf = os.urandom(self._partSize())
                    f.write(buf)
                    hashIn.update(buf)
            job = self.master.create(self.arbitraryJob)
            jobStoreFileID = self.master.writeFile(filePath, job.jobStoreID)
            os.unlink(filePath)
            self.master.readFile(jobStoreFileID, filePath)
            hashOut = hashlib.md5()
            with open(filePath, 'r') as f:
                while True:
                    buf = f.read(self._partSize())
                    if not buf:
                        break
                    hashOut.update(buf)
            self.assertEqual(hashIn.digest(), hashOut.digest())

        def assertUrl(self, url):
            prefix, path = url.split(':', 1)
            if prefix == 'file':
                self.assertTrue(os.path.exists(path))
            else:
                try:
                    urlopen(Request(url))
                except:
                    self.fail()

        @slow
        def testCleanCache(self):
            # Make a bunch of jobs
            master = self.master

            # Create parent job
            rootJob = master.createRootJob(self.arbitraryJob)
            # Create a bunch of child jobs
            for i in range(100):
                child = master.create(self.arbitraryJob)
                rootJob.stack.append([child])
            master.update(rootJob)

            # See how long it takes to clean with no cache
            noCacheStart = time.time()
            master.clean()
            noCacheEnd = time.time()

            noCacheTime = noCacheEnd - noCacheStart

            # See how long it takes to clean with cache
            jobCache = {jobGraph.jobStoreID: jobGraph
                        for jobGraph in master.jobs()}
            cacheStart = time.time()
            master.clean(jobCache)
            cacheEnd = time.time()

            cacheTime = cacheEnd - cacheStart

            logger.info("Without cache: %f, with cache: %f.", noCacheTime, cacheTime)

            # Running with the cache should be faster.
            self.assertTrue(cacheTime <= noCacheTime)

        @skip("too slow")  # This takes a long time on the remote JobStores
        def testManyJobs(self):
            # Make sure we can store large numbers of jobs

            # Make a bunch of jobs
            master = self.master

            # Create parent job
            rootJob = master.createRootJob(self.arbitraryJob)

            # Create a bunch of child jobs
            for i in range(3000):
                child = master.create(self.arbitraryJob)
                rootJob.stack.append(child)
            master.update(rootJob)

            # Pull them all back out again
            allJobs = list(master.jobs())

            # Make sure we have the right number of jobs. Cannot be precise because of limitations
            # on the jobs iterator for certain cloud providers
            self.assertTrue(len(allJobs) <= 3001)

        # NB: the 'thread' method seems to be needed here to actually
        # ensure the timeout is raised, probably because the only
        # "live" thread doesn't hold the GIL.
        @pytest.mark.timeout(45, method='thread')
        def testPartialReadFromStream(self):
            """Test whether readFileStream will deadlock on a partial read."""
            job = self.master.create(self.arbitraryJob)
            with self.master.writeFileStream(job.jobStoreID) as (f, fileID):
                # Write enough data to make sure the writer thread
                # will get blocked on the write. Technically anything
                # greater than the pipe buffer size plus the libc
                # buffer size (64K + 4K(?))  should trigger this bug,
                # but this gives us a lot of extra room just to be
                # sure.
                f.write('a' * 300000)
            with self.master.readFileStream(fileID) as f:
                self.assertEquals(f.read(1), "a")
            # If it times out here, there's a deadlock

        @abstractmethod
        def _corruptJobStore(self):
            """
            Deletes some part of the physical storage represented by a job store.
            """
            raise NotImplementedError()

        @slow
        def testDestructionOfCorruptedJobStore(self):
            self._corruptJobStore()
            worker = self._createJobStore()
            worker.destroy()
            # Note that self.master.destroy() is done as part of shutdown

        def testDestructionIdempotence(self):
            # Master is fully initialized
            self.master.destroy()
            # Create a second instance for the same physical storage but do not .initialize() or
            # .resume() it.
            cleaner = self._createJobStore()
            cleaner.destroy()
            # And repeat
            self.master.destroy()
            cleaner = self._createJobStore()
            cleaner.destroy()

        def testEmptyFileStoreIDIsReadable(self):
            """Simply creates an empty fileStoreID and attempts to read from it."""
            id = self.master.getEmptyFileStoreID()
            fh, path = tempfile.mkstemp()
            try:
                self.master.readFile(id, path)
                self.assertTrue(os.path.isfile(path))
            finally:
                os.unlink(path)

        def _largeLogEntrySize(self):
            """
            Sub-classes may want to override these in order to maximize test coverage
            """
            return 1 * 1024 * 1024

        def _batchDeletionSize(self):
            return 10

        def _partSize(self):
            return 5 * 1024 * 1024


class AbstractEncryptedJobStoreTest(object):
    # noinspection PyAbstractClass
    class Test(with_metaclass(ABCMeta, AbstractJobStoreTest.Test)):
        """
        A test of job stores that use encryption
        """

        def setUp(self):
            # noinspection PyAttributeOutsideInit
            self.sseKeyDir = tempfile.mkdtemp()
            # noinspection PyAttributeOutsideInit
            self.cseKeyDir = tempfile.mkdtemp()
            super(AbstractEncryptedJobStoreTest.Test, self).setUp()

        def tearDown(self):
            super(AbstractEncryptedJobStoreTest.Test, self).tearDown()
            shutil.rmtree(self.sseKeyDir)
            shutil.rmtree(self.cseKeyDir)

        def _createConfig(self):
            config = super(AbstractEncryptedJobStoreTest.Test, self)._createConfig()
            sseKeyFile = os.path.join(self.sseKeyDir, 'keyFile')
            with open(sseKeyFile, 'w') as f:
                f.write('01234567890123456789012345678901')
            config.sseKey = sseKeyFile
            # config.attrib['sse_key'] = sseKeyFile

            cseKeyFile = os.path.join(self.cseKeyDir, 'keyFile')
            with open(cseKeyFile, 'w') as f:
                f.write("i am a fake key, so don't use me")
            config.cseKey = cseKeyFile
            return config


class FileJobStoreTest(AbstractJobStoreTest.Test):
    def _createJobStore(self):
        return FileJobStore(self.namePrefix)

    def _corruptJobStore(self):
        assert isinstance(self.master, FileJobStore)  # type hint
        shutil.rmtree(self.master.jobStoreDir)

    def _prepareTestFile(self, dirPath, size=None):
        fileName = 'testfile_%s' % uuid.uuid4()
        localFilePath = dirPath + fileName
        url = 'file://%s' % localFilePath
        if size is None:
            return url
        else:
            content = os.urandom(size)
            with open(localFilePath, 'w') as writable:
                writable.write(content)

            return url, hashlib.md5(content).hexdigest()

    def _hashTestFile(self, url):
        localFilePath = FileJobStore._extractPathFromUrl(urlparse.urlparse(url))
        with open(localFilePath, 'r') as f:
            return hashlib.md5(f.read()).hexdigest()

    def _createExternalStore(self):
        return tempfile.mkdtemp()

    def _cleanUpExternalStore(self, dirPath):
        shutil.rmtree(dirPath)


@experimental
@needs_google
class GoogleJobStoreTest(AbstractJobStoreTest.Test):
    projectID = os.getenv('TOIL_GOOGLE_PROJECTID')
    headers = {"x-goog-project-id": projectID}

    def _createJobStore(self):
        from toil.jobStores.googleJobStore import GoogleJobStore
        return GoogleJobStore.initialize(GoogleJobStoreTest.projectID + ":" + self.namePrefix)

    def _corruptJobStore(self):
        # The Google job store has only one resource, the bucket, so we can't corrupt it without
        # fully deleting it.
        pass

    def _prepareTestFile(self, bucket, size=None):
        import boto
        fileName = 'testfile_%s' % uuid.uuid4()
        uri = 'gs://%s/%s' % (bucket.name, fileName)
        if size:
            with open('/dev/urandom', 'r') as readable:
                boto.storage_uri(uri).set_contents_from_string(readable.read(size))
        return uri

    def _hashTestFile(self, url):
        import boto
        from toil.jobStores.googleJobStore import GoogleJobStore
        projectID, uri = GoogleJobStore._getResources(urlparse.urlparse(url))
        uri = boto.storage_uri(uri)
        contents = uri.get_contents_as_string(headers=self.headers)
        return hashlib.md5(contents).hexdigest()

    def _createExternalStore(self):
        import boto
        from toil.jobStores.googleJobStore import GoogleJobStore
        uriString = "gs://import-export-test-%s" % str(uuid.uuid4())
        uri = boto.storage_uri(uriString)
        return GoogleJobStore._retryCreateBucket(uri=uri, headers=self.headers)

    def _cleanUpExternalStore(self, bucket):
        import boto
        while True:
            for key in bucket.list():
                try:
                    key.delete()
                except boto.exception.GSResponseError as e:
                    if e.status == 404:
                        pass
                    else:
                        raise
            try:
                bucket.delete()
            except boto.exception.GSResponseError as e:
                if e.status == 404:
                    break
                else:
                    continue


@needs_aws
class AWSJobStoreTest(AbstractJobStoreTest.Test):

    def _createJobStore(self):
        from toil.jobStores.aws.jobStore import AWSJobStore
        partSize = self._partSize()
        for encrypted in (True, False):
            self.assertTrue(AWSJobStore.FileInfo.maxInlinedSize(encrypted) < partSize)
        return AWSJobStore(self.awsRegion()+ ':' + self.namePrefix, partSize=partSize)

    def _corruptJobStore(self):
        from toil.jobStores.aws.jobStore import AWSJobStore
        assert isinstance(self.master, AWSJobStore)  # type hinting
        self.master.filesBucket.delete()

    def testSDBDomainsDeletedOnFailedJobstoreBucketCreation(self):
        """
        This test ensures that SDB domains bound to a jobstore are deleted if the jobstore bucket
        failed to be created.  We simulate a failed jobstore bucket creation by using a bucket in a
        different region with the same name.
        """
        from boto.sdb import connect_to_region
        from boto.s3.connection import Location, S3Connection
        from toil.jobStores.aws.jobStore import BucketLocationConflictException
        from toil.jobStores.aws.utils import retry_s3
        externalAWSLocation = Location.USWest
        for testRegion in 'us-east-1', 'us-west-2':
            # We run this test twice, once with the default s3 server us-east-1 as the test region
            # and once with another server (us-west-2).  The external server is always us-west-1.
            # This incidentally tests that the BucketLocationConflictException is thrown when using
            # both the default, and a non-default server.
            testJobStoreUUID = str(uuid.uuid4())
            # Create the nucket at the external region
            s3 = S3Connection()
            for attempt in retry_s3(delays=(2,5,10,30,60), timeout=600):
                with attempt:
                    bucket = s3.create_bucket('domain-test-' + testJobStoreUUID + '--files',
                                              location=externalAWSLocation)
            options = Job.Runner.getDefaultOptions('aws:' + testRegion + ':domain-test-' +
                                                   testJobStoreUUID)
            options.logLevel = 'DEBUG'
            try:
                with Toil(options) as toil:
                    pass
            except BucketLocationConflictException:
                # Catch the expected BucketLocationConflictException and ensure that the bound
                # domains don't exist in SDB.
                sdb = connect_to_region(self.awsRegion())
                next_token = None
                allDomainNames = []
                while True:
                    domains = sdb.get_all_domains(max_domains=100, next_token=next_token)
                    allDomainNames.extend([x.name for x in domains])
                    next_token = domains.next_token
                    if next_token is None:
                        break
                self.assertFalse([d for d in allDomainNames if testJobStoreUUID in d])
            else:
                self.fail()
            finally:
                for attempt in retry_s3():
                    with attempt:
                        s3.delete_bucket(bucket=bucket)

    @slow
    def testInlinedFiles(self):
        from toil.jobStores.aws.jobStore import AWSJobStore
        master = self.master
        for encrypted in (True, False):
            n = AWSJobStore.FileInfo.maxInlinedSize(encrypted)
            sizes = (1, old_div(n, 2), n - 1, n, n + 1, 2 * n)
            for size in chain(sizes, islice(reversed(sizes), 1)):
                s = os.urandom(size)
                with master.writeSharedFileStream('foo') as f:
                    f.write(s)
                with master.readSharedFileStream('foo') as f:
                    self.assertEqual(s, f.read())

    def testInaccessableLocation(self):
        url = 's3://toil-no-location-bucket-dont-delete/README'
        with patch('toil.jobStores.aws.jobStore.log') as mock_log:
            jobStoreID = self.master.importFile(url)
            self.assertTrue(self.master.fileExists(jobStoreID))
            args, kwargs = mock_log.warn.call_args
            self.assertTrue('Could not determine location' in args[0])

    @slow
    def testMultiPartImportFailures(self):
        # This should be less than the number of threads in the pool used by the MP copy.
        num_parts = 10
        i = count()

        # noinspection PyUnusedLocal
        def fail(*args, **kwargs):
            # The sleep ensures that all tasks are scheduled in the thread pool. Without it,
            # there is a chance that one task fails before another is scheduled, causing the
            # latter to bail out immediatly and failing the assertion that ensure the number of
            # failing tasks.
            time.sleep(.25)
            if next(i) % 2 == 0:
                raise RuntimeError()

        with patch('boto.s3.multipart.MultiPartUpload.copy_part_from_key',
                   new_callable=lambda: fail):
            self.master.partSize = self.mpTestPartSize
            bucket = self._externalStore()
            url, md5 = self._prepareTestFile(bucket, self.mpTestPartSize * num_parts)
            try:
                self.master.importFile(url)
            except RuntimeError as e:
                self.assertEquals(e.message, 'Failed to copy at least %d part(s)' % (old_div(num_parts, 2)))
            else:
                self.fail('Expected a RuntimeError to be raised')
    def testOverlargeJob(self):
        master = self.master
        masterRequirements = dict(memory=12, cores=34, disk=35, preemptable=True)
        overlargeJobNodeOnMaster = JobNode(command='master-overlarge',
                                    requirements=masterRequirements,
                                    jobName='test-overlarge', unitName='onMaster',
                                    jobStoreID=None, predecessorNumber=0)

        #Make the pickled size of the job larger than 256K
        with open("/dev/urandom", "r") as random:
            overlargeJobNodeOnMaster.jobName = random.read(512 * 1024)
        overlargeJobOnMaster = master.create(overlargeJobNodeOnMaster)
        self.assertTrue(master.exists(overlargeJobOnMaster.jobStoreID))
        overlargeJobOnMasterDownloaded = master.load(overlargeJobOnMaster.jobStoreID)
        jobsOnMaster = [job for job in master.jobs()]
        self.assertEqual(jobsOnMaster, [overlargeJobOnMaster])
        master.delete(overlargeJobOnMaster.jobStoreID)

    def _prepareTestFile(self, bucket, size=None):
        fileName = 'testfile_%s' % uuid.uuid4()
        url = 's3://%s/%s' % (bucket.name, fileName)
        if size is None:
            return url
        with open('/dev/urandom', 'r') as readable:
            bucket.new_key(fileName).set_contents_from_string(readable.read(size))
        return url, hashlib.md5(bucket.get_key(fileName).get_contents_as_string()).hexdigest()

    def _hashTestFile(self, url):
        from toil.jobStores.aws.jobStore import AWSJobStore
        key = AWSJobStore._getKeyForUrl(urlparse.urlparse(url), existing=True)
        try:
            contents = key.get_contents_as_string()
        finally:
            key.bucket.connection.close()
        return hashlib.md5(contents).hexdigest()

    def _createExternalStore(self):
        import boto.s3
        s3 = boto.s3.connect_to_region(self.awsRegion())
        try:
            return s3.create_bucket(bucket_name='import-export-test-%s' % uuid.uuid4(),
                                    location=region_to_bucket_location(self.awsRegion()))
        except:
            with panic(log=logger):
                s3.close()

    def _cleanUpExternalStore(self, bucket):
        try:
            for key in bucket.list():
                key.delete()
            bucket.delete()
        finally:
            bucket.connection.close()

    def _largeLogEntrySize(self):
        from toil.jobStores.aws.jobStore import AWSJobStore
        # So we get into the else branch of reader() in uploadStream(multiPart=False):
        return AWSJobStore.FileInfo.maxBinarySize() * 2

    def _batchDeletionSize(self):
        from toil.jobStores.aws.jobStore import AWSJobStore
        return AWSJobStore.itemsPerBatchDelete


@needs_aws
class InvalidAWSJobStoreTest(ToilTest):
    def testInvalidJobStoreName(self):
        from toil.jobStores.aws.jobStore import AWSJobStore
        self.assertRaises(ValueError,
                          AWSJobStore,
                          'us-west-2:a--b')
        self.assertRaises(ValueError,
                          AWSJobStore,
                          'us-west-2:' + ('a' * 100))
        self.assertRaises(ValueError,
                          AWSJobStore,
                          'us-west-2:a_b')


@needs_azure
class AzureJobStoreTest(AbstractJobStoreTest.Test):
    accountName = os.getenv('TOIL_AZURE_KEYNAME')

    def _createJobStore(self):
        from toil.jobStores.azureJobStore import AzureJobStore
        return AzureJobStore(self.accountName + ':' + self.namePrefix)

    def _corruptJobStore(self):
        from toil.jobStores.azureJobStore import AzureJobStore
        assert isinstance(self.master, AzureJobStore)  # type hinting
        self.master.tableService.delete_table(self.master.jobFileIDs)

    def _partSize(self):
        from toil.jobStores.azureJobStore import AzureJobStore
        return AzureJobStore._maxAzureBlockBytes

    def testLargeJob(self):
        from toil.jobStores.azureJobStore import maxAzureTablePropertySize
        command = os.urandom(maxAzureTablePropertySize * 2)
        jobNode1 = self.arbitraryJob
        jobNode1.command=command
        job1 = self.master.create(jobNode1)
        self.assertEqual(job1.command, command)
        job2 = self.master.load(job1.jobStoreID)
        self.assertIsNot(job1, job2)
        self.assertEqual(job2.command, command)

    def testJobStoreExists(self):
        from toil.jobStores.azureJobStore import AzureJobStore
        assert isinstance(self.master, AzureJobStore)  # mostly for type hinting
        self.assertTrue(self.master._jobStoreExists())
        self.master.destroy()
        self.assertFalse(self.master._jobStoreExists())

    def _prepareTestFile(self, containerName, size=None):
        from toil.jobStores.azureJobStore import _fetchAzureAccountKey
        from azure.storage.blob import BlobService

        fileName = 'testfile_%s' % uuid.uuid4()
        url = 'wasb://%s@%s.blob.core.windows.net/%s' % (containerName, self.accountName, fileName)
        if size is None:
            return url
        blobService = BlobService(account_key=_fetchAzureAccountKey(self.accountName),
                                  account_name=self.accountName)
        content = os.urandom(size)
        blobService.put_block_blob_from_text(containerName, fileName, content)
        return url, hashlib.md5(content).hexdigest()

    def _hashTestFile(self, url):
        from toil.jobStores.azureJobStore import AzureJobStore, retry_azure
        url = urlparse.urlparse(url)
        blob = AzureJobStore._parseWasbUrl(url)
        for attempt in retry_azure():
            with attempt:
                content = blob.service.get_blob_to_bytes(blob.container, blob.name)
                return hashlib.md5(content).hexdigest()

    def _createExternalStore(self):
        from toil.jobStores.azureJobStore import _fetchAzureAccountKey
        from azure.storage.blob import BlobService

        blobService = BlobService(account_key=_fetchAzureAccountKey(self.accountName),
                                  account_name=self.accountName)
        containerName = 'import-export-test-%s' % uuid.uuid4()
        blobService.create_container(containerName)
        return containerName

    def _cleanUpExternalStore(self, containerName):
        from toil.jobStores.azureJobStore import _fetchAzureAccountKey
        from azure.storage.blob import BlobService
        blobService = BlobService(account_key=_fetchAzureAccountKey(self.accountName),
                                  account_name=self.accountName)
        blobService.delete_container(containerName)


@needs_azure
class InvalidAzureJobStoreTest(ToilTest):
    def testInvalidJobStoreName(self):
        from toil.jobStores.azureJobStore import AzureJobStore
        self.assertRaises(ValueError,
                          AzureJobStore,
                          'toiltest:a--b')
        self.assertRaises(ValueError,
                          AzureJobStore,
                          'toiltest:' + ('a' * 100))
        self.assertRaises(ValueError,
                          AzureJobStore,
                          'toiltest:a_b')


class EncryptedFileJobStoreTest(FileJobStoreTest, AbstractEncryptedJobStoreTest.Test):
    pass


@needs_aws
@needs_encryption
@slow
class EncryptedAWSJobStoreTest(AWSJobStoreTest, AbstractEncryptedJobStoreTest.Test):
    pass


@needs_azure
@needs_encryption
@slow
class EncryptedAzureJobStoreTest(AzureJobStoreTest, AbstractEncryptedJobStoreTest.Test):
    pass


class StubHttpRequestHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    fileContents = 'A good programmer looks both ways before crossing a one-way street'
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-length", len(self.fileContents))
        self.end_headers()
        self.wfile.write(self.fileContents)


AbstractJobStoreTest.Test.makeImportExportTests()
