#!/usr/bin/env python

import hashlib
import commands
from lxml import etree
from Queue import Queue
from threading import Thread, Lock
from datetime import datetime
import hdfs

# import the Celery app context
from crawl.celery import app
from crawl.celery import cfg

# import the Celery log getter and use it
from celery.utils.log import get_task_logger
logger = get_task_logger(__name__)


METS= "{http://www.loc.gov/METS/}"
MODS = "{http://www.loc.gov/mods/v3}"
PREMIS = "{info:lc/xmlns/premis-v2}"
WCT = "{http://www.bl.uk/namespaces/wct}"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}"
XLINK = "{http://www.w3.org/1999/xlink}"
schemaLocation = "http://www.loc.gov/METS/ http://www.loc.gov/standards/mets/mets.xsd http://www.loc.gov/mods/v3 http://www.loc.gov/standards/mods/v3/mods-3-3.xsd info:lc/xmlns/premis-v2 http://www.loc.gov/standards/premis/premis.xsd http://www.w3.org/1999/xlink http://www.loc.gov/standards/xlink/xlink.xsd"

WEBHDFS_PREFIX="http://dls.httpfs.wa.bl.uk:14000/webhdfs/v1"
WEBHDFS_SUFFIX="?user.name=hadoop&op=OPEN"

PERMISSION_STATE="Granted"
PERMISSION_START_DATE="2013-04-06"
PERMISSION_NAME="The Legal Deposit Libraries (Non-Print Works) Regulations 2013"
PERMISSION_PUBLISHED="True"
HERITRIX = "heritrix-3.3.0-SNAPSHOT+uk.bl.wap"
SOFTWARE_VERSION="python-shepherd=1.0.0"
CLAMDSCAN = "ClamAV 0.98.3/22066/Thu Aug 11 08:15:04 2016"

NUM_THREADS=10


def calculateHash( path ):
    logger.info("Starting to generate hash for %s" % path)
    client = hdfs.InsecureClient(cfg.get('hdfs', 'url'), user=cfg.get('hdfs', 'user'))
    sha = hashlib.sha512()
    with client.read(path) as file:
        while True:
            data = file.read( 10485760 )
            if not data:
                file.close()
                break
            sha.update( data )
    logger.info("Finished generating hash for %s" % path)
    return sha.hexdigest()

def getLength( path ):
    client = hdfs.InsecureClient(cfg.get('hdfs', 'url'), user=cfg.get('hdfs', 'user'))
    status = client.status(path)
    return status['length']

def getModifiedDate( path, client ):
    logger.info("Running getModifiedDate on %s" % path)
    status = client.status(path)
    logger.debug(status)
    return status['modificationTime']/1000.0

count = 1
counter_lock = Lock()
def getCount():
    counter_lock.acquire()
    try:
        global count
        val = "%04d" % count
        count += 1
        return val
    finally:
        counter_lock.release()


def create_warcs(q, warcs, parent):
        while True:
            w = Warc(q.get(), parent)
            warcs.append(w)
            q.task_done()


class Warc:
    def __init__( self, path, parent):
        self.path = path
        self.admid = getCount()
        if path in parent.hash_cache:
            (sha, len) = parent.hash_cache.get(path).split(" ")
            self.hash = sha
            self.size = len
        else:
            self.hash = calculateHash( path )
            self.size = getLength( path )
        logger.debug("%s %s %s %s" % (self.path, self.hash, self.size, self.admid))


class ZipContainer:
    def __init__( self, path, parent ):
        self.path = path
        self.admid = getCount()
        if path in parent.hash_cache:
            (sha, len) = parent.hash_cache.get(path).split(" ")
            self.hash = sha
            self.size = len
        else:
            self.hash = calculateHash( path )
            self.size = getLength( path )
        logger.debug("%s %s %s %s" % (self.path, self.hash, self.size, self.admid))


class Mets:
    def __init__( self, date, warcs, viral, logs, identifiers, hash_cache=None, client=None ):
        if client is None:
            self.client=  hdfs.InsecureClient(cfg.get('hdfs','url'), user=cfg.get('hdfs','user'))
        else:
            self.client = client
        self.warcs = []
        self.viral = []
        self.date = date
        self.wq = Queue()
        self.vq = Queue()
        self.hash_cache = hash_cache

        for i in range(NUM_THREADS):
            worker = Thread(target=create_warcs, args=(self.wq, self.warcs, self))
            worker.setDaemon(True)
            worker.start()

        for warc in warcs:
            self.wq.put(warc)
        self.wq.join()

        for i in range(NUM_THREADS):
            worker = Thread(target=create_warcs, args=(self.vq, self.viral, self))
            worker.setDaemon(True)
            worker.start()

        for warc in viral:
            self.vq.put(warc)
        self.vq.join()

        self.logs = []
        for log in logs:
            self.logs.append( ZipContainer( path=log, parent=self ))
        self.identifiers = identifiers
        self.createDomainMets()
        self.createCrawlerMets()

    def getXml( self ):
        return etree.tostring( self.mets, pretty_print=True, xml_declaration=True, encoding="UTF-8" )

    def createDomainMets( self ):
        etree.register_namespace( "mets", "http://www.loc.gov/METS/" )
        etree.register_namespace( "mods", "http://www.loc.gov/mods/v3" )
        etree.register_namespace( "premis", "info:lc/xmlns/premis-v2" )
        etree.register_namespace( "wct", "http://www.bl.uk/namespaces/wct" )
        etree.register_namespace( "xsi", "http://www.w3.org/2001/XMLSchema-instance" )
        etree.register_namespace( "xlink", "http://www.w3.org/1999/xlink" )
        self.mets = etree.Element( METS + "mets", TYPE="webarchive_domain", attrib={ XSI + "schemaLocation" : schemaLocation } )

        metsHdr = etree.SubElement( self.mets, METS + "metsHdr", CREATEDATE=datetime.now().strftime( "%Y-%m-%dT%H:%M:%SZ" ) )
        agent = etree.SubElement( metsHdr, METS + "agent", ROLE="CREATOR", TYPE="OTHER", OTHERTYPE="software" )
        name = etree.SubElement( agent, METS + "name" )
        name.text = SOFTWARE_VERSION

        amdSec = etree.SubElement( self.mets, METS + "amdSec", ID="AMD0000" )
        rightsMD = etree.SubElement( amdSec, METS + "rightsMD", ID="DP0000" )
        mdWrap = etree.SubElement( rightsMD, METS + "mdWrap", MDTYPE="OTHER", OTHERMDTYPE="wctpermissions" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        permissions = etree.SubElement( xmlData, WCT + "Permissions" )
        permission = etree.SubElement( permissions, WCT + "Permission" )
        state = etree.SubElement( permission, WCT + "State" )
        state.text = PERMISSION_STATE
        startdate = etree.SubElement( permission, WCT + "StartDate" )
        startdate.text = PERMISSION_START_DATE
        harvestauthorization = etree.SubElement( permission, WCT + "HarvestAuthorization" )
        name = etree.SubElement( harvestauthorization, WCT + "Name" )
        name.text = PERMISSION_NAME
        ispublished = etree.SubElement(harvestauthorization, WCT + "IsPublished" )
        ispublished.text = PERMISSION_PUBLISHED

        digiprovMD = etree.SubElement( amdSec, METS + "digiprovMD", ID="DP0001" )
        mdWrap = etree.SubElement( digiprovMD, METS + "mdWrap", MDTYPE="PREMIS:EVENT" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        event = etree.SubElement( xmlData, PREMIS + "event" )
        eventIdentifier = etree.SubElement( event, PREMIS + "eventIdentifier" )
        eventIdentifierType = etree.SubElement( eventIdentifier, PREMIS + "eventIdentifierType" )
        eventIdentifierType.text = "local"
        eventIdentifierValue = etree.SubElement( eventIdentifier, PREMIS + "eventIdentifierValue" )
        eventIdentifierValue.text = "EVENT0000"
        eventType = etree.SubElement( event, PREMIS + "eventType" )
        eventType.text = "crawlstart"
        eventDateTime = etree.SubElement( event, PREMIS + "eventDateTime" )
        eventDateTime.text = self.date
        linkingAgentIdentifier = etree.SubElement( event, PREMIS + "linkingAgentIdentifier" )
        linkingAgentIdentifierType = etree.SubElement( linkingAgentIdentifier, PREMIS + "linkingAgentIdentifierType" )
        linkingAgentIdentifierType.text = "local"
        linkingAgentIdentifierValue = etree.SubElement( linkingAgentIdentifier, PREMIS + "linkingAgentIdentifierValue" )
        linkingAgentIdentifierValue.text = "AGENT0000"

        digiprovMD = etree.SubElement( amdSec, METS + "digiprovMD", ID="DP0002" )
        mdWrap = etree.SubElement( digiprovMD, METS + "mdWrap", MDTYPE="PREMIS:AGENT" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        agent = etree.SubElement( xmlData, PREMIS + "agent" )
        agentIdentifier = etree.SubElement( agent, PREMIS + "agentIdentifier" )
        agentIdentifierType = etree.SubElement( agentIdentifier, PREMIS + "agentIdentifierType" )
        agentIdentifierType.text = "local"
        agentIdentifierValue = etree.SubElement( agentIdentifier, PREMIS + "agentIdentifierValue" )
        agentIdentifierValue.text = "AGENT0000"
        agentName = etree.SubElement( agent, PREMIS + "agentName" )
        agentName.text = HERITRIX
        agentType = etree.SubElement( agent, PREMIS + "agentType" )
        agentType.text = "software"

    def buildZipPremis( self, zip, identifier ):
        amdSec = etree.SubElement( self.mets, METS + "amdSec", ID="AMDZIP" + zip.admid )
        digiprovMD = etree.SubElement( amdSec, METS + "digiprovMD", ID="DMDZIP" + zip.admid )
        mdWrap = etree.SubElement( digiprovMD, METS + "mdWrap", MDTYPE="PREMIS:OBJECT" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        object = etree.SubElement( xmlData, PREMIS + "object" )
        object.set( XSI + "type", "premis:file" )
        objectIdentifier = etree.SubElement( object, PREMIS + "objectIdentifier" )
        objectIdentifierType = etree.SubElement( objectIdentifier, PREMIS + "objectIdentifierType" )
        objectIdentifierType.text = "ARK"
        objectIdentifierValue = etree.SubElement( objectIdentifier, PREMIS + "objectIdentifierValue" )
        objectIdentifierValue.text = identifier
        objectCharacteristics = etree.SubElement( object, PREMIS + "objectCharacteristics" )
        compositionLevel = etree.SubElement( objectCharacteristics, PREMIS + "compositionLevel" )
        compositionLevel.text = "1" 
        fixity = etree.SubElement( objectCharacteristics, PREMIS + "fixity" )
        messageDigestAlgorithm = etree.SubElement( fixity, PREMIS + "messageDigestAlgorithm" )
        messageDigestAlgorithm.text = "SHA-512"
        messageDigest = etree.SubElement( fixity, PREMIS + "messageDigest" )
        messageDigest.text = zip.hash
        size = etree.SubElement( objectCharacteristics, PREMIS + "size" )
        size.text = str( zip.size )
        format = etree.SubElement( objectCharacteristics, PREMIS + "format" )
        formatDesignation = etree.SubElement( format, PREMIS + "formatDesignation" )
        formatName = etree.SubElement( formatDesignation, PREMIS + "formatName" )
        formatName.text = "application/zip"

    def buildPremis( self, warc, identifier, virus=False ):
        amdSec = etree.SubElement( self.mets, METS + "amdSec", ID="AMDWARC" + warc.admid )
        digiprovMD = etree.SubElement( amdSec, METS + "digiprovMD", ID="DMDWARC" + warc.admid )
        mdWrap = etree.SubElement( digiprovMD, METS + "mdWrap", MDTYPE="PREMIS:OBJECT" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        object = etree.SubElement( xmlData, PREMIS + "object" )
        object.set( XSI + "type", "premis:file" )
        objectIdentifier = etree.SubElement( object, PREMIS + "objectIdentifier" )
        objectIdentifierType = etree.SubElement( objectIdentifier, PREMIS + "objectIdentifierType" )
        objectIdentifierType.text = "ARK"
        objectIdentifierValue = etree.SubElement( objectIdentifier, PREMIS + "objectIdentifierValue" )
        objectIdentifierValue.text = identifier
        objectCharacteristics = etree.SubElement( object, PREMIS + "objectCharacteristics" )
        compositionLevel = etree.SubElement( objectCharacteristics, PREMIS + "compositionLevel" )
        compositionLevel.text = "1" 
        fixity = etree.SubElement( objectCharacteristics, PREMIS + "fixity" )
        messageDigestAlgorithm = etree.SubElement( fixity, PREMIS + "messageDigestAlgorithm" )
        messageDigestAlgorithm.text = "SHA-512"
        messageDigest = etree.SubElement( fixity, PREMIS + "messageDigest" )
        messageDigest.text = warc.hash
        size = etree.SubElement( objectCharacteristics, PREMIS + "size" )
        size.text = str( warc.size )
        format = etree.SubElement( objectCharacteristics, PREMIS + "format" )
        formatDesignation = etree.SubElement( format, PREMIS + "formatDesignation" )
        formatName = etree.SubElement( formatDesignation, PREMIS + "formatName" )
        formatName.text = "application/warc"

        digiprovMD = etree.SubElement( amdSec, METS + "digiprovMD", ID="DMDWARC" + warc.admid + "_EVENT" )
        mdWrap = etree.SubElement( digiprovMD, METS + "mdWrap", MDTYPE="PREMIS:EVENT" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        event = etree.SubElement( xmlData, PREMIS + "event" )
        eventIdentifier = etree.SubElement( event, PREMIS + "eventIdentifier" )
        eventIdentifierType = etree.SubElement( eventIdentifier, PREMIS + "eventIdentifierType" )
        eventIdentifierType.text = "local"
        eventIdentifierValue = etree.SubElement( eventIdentifier, PREMIS + "eventIdentifierValue" )
        eventIdentifierValue.text = "EVENT" + warc.admid
        eventType = etree.SubElement( event, PREMIS + "eventType" )
        eventType.text = "virusCheck"
        eventDateTime = etree.SubElement( event, PREMIS + "eventDateTime" )
        eventDateTime.text = datetime.fromtimestamp( getModifiedDate( warc.path, self.client ) ).strftime( "%Y-%m-%dT%H:%M:%S" )
        eventOutcomeInformation = etree.SubElement( event, PREMIS + "eventOutcomeInformation" )
        eventOutcome = etree.SubElement( eventOutcomeInformation, PREMIS + "eventOutcome" )
        if( virus ):
            eventOutcome.text = "viral, failed but forced"
        else:
            eventOutcome.text = "no virus detected"
        linkingAgentIdentifier = etree.SubElement( event, PREMIS + "linkingAgentIdentifier" )
        linkingAgentIdentifierType = etree.SubElement( linkingAgentIdentifier, PREMIS + "linkingAgentIdentifierType" )
        linkingAgentIdentifierType.text = "local"
        linkingAgentIdentifierValue = etree.SubElement( linkingAgentIdentifier, PREMIS + "linkingAgentIdentifierValue" )
        linkingAgentIdentifierValue.text = "AGENT" + warc.admid

        digiprovMD = etree.SubElement( amdSec, METS + "digiprovMD", ID="DMDWARC" + warc.admid + "_AGENT" )
        mdWrap = etree.SubElement( digiprovMD, METS + "mdWrap", MDTYPE="PREMIS:AGENT" )
        xmlData = etree.SubElement( mdWrap, METS + "xmlData" )
        agent = etree.SubElement( xmlData, PREMIS + "agent" )
        agentIdentifier = etree.SubElement( agent, PREMIS + "agentIdentifier" )
        agentIdentifierType = etree.SubElement( agentIdentifier, PREMIS + "agentIdentifierType" )
        agentIdentifierType.text = "local"
        agentIdentifierValue = etree.SubElement( agentIdentifier, PREMIS + "agentIdentifierValue" )
        agentIdentifierValue.text = "AGENT" + warc.admid
        agentName = etree.SubElement( agent, PREMIS + "agentName" )
        agentName.text = CLAMDSCAN
        agentType = etree.SubElement( agent, PREMIS + "agentType" )
        agentType.text = "software"

    def buildWarcFileGrp( self, root, files, use, subdir="/warcs/" ):
        fileGrp = etree.SubElement( root, METS + "fileGrp", USE=use )
        for warc in files:
            metsFile = etree.SubElement( fileGrp, METS + "file", ID="WARC" + warc.admid, ADMID="AMDWARC" + warc.admid, SIZE=str( warc.size ), CHECKSUM=warc.hash, CHECKSUMTYPE="SHA-512", MIMETYPE="application/warc" )
            fLocat = etree.SubElement( metsFile, METS + "FLocat", LOCTYPE="URL",  )
            fLocat.set( XLINK + "href", "%s%s%s" % ( WEBHDFS_PREFIX, warc.path, WEBHDFS_SUFFIX ) )
            transformFile = etree.SubElement( metsFile, METS + "transformFile", TRANSFORMTYPE="decompression", TRANSFORMALGORITHM="WARC", TRANSFORMORDER="1" )

    def buildLogFileGrp( self, root ):
        fileGrp = etree.SubElement( root, METS + "fileGrp", USE="Logfiles" )
        for zip in self.logs:
            metsFile = etree.SubElement( fileGrp, METS + "file", ID="ZIP" + zip.admid, ADMID="AMDZIP" + zip.admid, SIZE=str( zip.size ), CHECKSUM=zip.hash, CHECKSUMTYPE="SHA-512", MIMETYPE="application/zip" )
            fLocat = etree.SubElement( metsFile, METS + "FLocat", LOCTYPE="URL",  )
            fLocat.set( XLINK + "href", "%s%s%s" % ( WEBHDFS_PREFIX, zip.path, WEBHDFS_SUFFIX ) )
            transformFile = etree.SubElement( metsFile, METS + "transformFile", TRANSFORMTYPE="decompression", TRANSFORMALGORITHM="ZIP", TRANSFORMORDER="1" )

    def buildStructMap( self ):
        structMap = etree.SubElement( self.mets, METS + "structMap", TYPE="logical" )
        div = etree.SubElement( structMap, METS + "div", ID="div0000", TYPE="uk-web-domain", ADMID="AMD0000" )
        for warc in self.warcs:
            fptr = etree.SubElement( div, METS + "fptr", FILEID="WARC" + warc.admid )
        for viral in self.viral:
            fptr = etree.SubElement( div, METS + "fptr", FILEID="WARC" + viral.admid )
        for zip in self.logs:
            fptr = etree.SubElement( div, METS + "fptr", FILEID="ZIP" + zip.admid )
        

    def createCrawlerMets( self ):
        i = 0
        for warc in self.warcs:
            i += 1
            print("Running building PREMIS for WARC %i/%i: %s" % ( i, len(self.warcs), warc.path))
            self.buildPremis( warc, self.identifiers.pop() )

        i = 0
        for warc in self.viral:
            i += 1
            print("Running building PREMIS for viral WARC %i/%i: %s" % ( i, len(self.viral), warc.path))
            self.buildPremis( warc, self.identifiers.pop(), virus=True )
        
        i = 0
        for zip in self.logs:
            i += 1
            print("Running building PREMIS for ZIP %i/%i: %s" % ( i, len(self.logs), zip.path))
            self.buildZipPremis( zip, self.identifiers.pop() )

        fileSec = etree.SubElement( self.mets, METS + "fileSec" )
        self.buildWarcFileGrp( fileSec, self.warcs, "DigitalManifestation" )
        self.buildWarcFileGrp( fileSec, self.viral, "ViralFiles", "/viral/" )
        self.buildLogFileGrp( fileSec )
        self.buildStructMap()
