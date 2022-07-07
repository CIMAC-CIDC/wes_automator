#!/usr/bin/env python
"""Len Taing 2022 (TGBTG)
WES Monitor: Program to automate wes_automator runs in BATCHES

WES monitor takes in a spreadsheet with an expected format:
tumor_cimac_id,normal_cimac_id,google_bucket_path,tumor_fastq_path_pair1,tumor_fastq_path_pair2,normal_fastq_path_pair1,normal_fastq_path_pair2,rna_bam_file,rna_expression_file,cimac_center,cores,disk_size,wes_commit,image,wes_ref_snapshot,somatic_caller,trim_soft_clip

The program then will:
1. parse the xlsx runs, 
2. generate a wes automator config.yaml *file* for each run, 
3. call wes_automator to fire off the run (using the generated config)
4. repeat step 3 for other runs UP to max GCP cores (500)
5. receive a. heartbeat messages and b. snakemake workflow updates from each
   run.  The heartbeat messages tell us that the instance is still running;
   The snakemake messages tell us the progress of each wes run
6. output the status of each wes run to the terminal screen

7. When an wes run completes OR errors out, wes monitor will: 
   a. stop the completed/errored instance
   b. start the next wes run on the run queue if any (up to the 500 core limit)
8. When all runs are complete, wes_monitor will:
   1. print the last state of each instance, complete/error; and exit
      This message allows the user to ingest completed runs, or debug error
      runs

NEXT iteration: wes monitor will auto-ingest and auto-stash versioning info
for complete runs, and then delete the instance
"""

import os
import sys
import time
import re
import threading
import subprocess
from time import sleep
import math
from optparse import OptionParser

import googleapiclient.discovery

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import SingleQuotedScalarString

import wes_automator
import instance

from openpyxl.reader.excel import load_workbook

###############################################################################
# WEB API with FLASK
###############################################################################
from flask import Flask
from flask_restful import Api, Resource, request, abort #reqparse, abort, fields, marshal_with
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.pool import NullPool
from sqlalchemy import or_, and_
import logging

import db_manager

#GLOBALS
app = Flask(__name__)
api = Api(app)
_db_filename="wes_monitor.sqlite.db"
#app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///%s?check_same_thread=False' % _db_filename
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///%s' % _db_filename
#db = SQLAlchemy(app, engine_options={'poolclass':NullPool})
db = SQLAlchemy(app)
db_manager.init_engine(app.config['SQLALCHEMY_DATABASE_URI'])
db_manager.init_session_factory()

logging.basicConfig(filename='wes_monitor.log', level=logging.DEBUG, format=f'%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s')

#use a template to build run-specific configs
_wes_automator_template = "config.automator.template.yaml"
#google cloud authenticated compute instance--used in instance.stop call below
_compute = googleapiclient.discovery.build('compute', 'v1')
_project = "cidc-biofx"

#define a /register/<instance_name>/?num_steps=
###############################################################################
#KEY NOTE: any writes to the db must use thread-safe sessions --
#PATTERN:
#with db_manager.ManagedSession() as session:
#    wes_run = session.query(WesRun).filter(WesRun.instance_name == instance_name).first()
#    #UPDATE wes_run object
#    session.commit()
###############################################################################

class Update(Resource):
    def put(self, instance_name):
        #NOTE: we need to use thread-safe methods here
        with db_manager.ManagedSession() as session:
            wes_run = session.query(WesRun).filter(WesRun.instance_name == instance_name).first()
            if wes_run:
                if 'num_steps' in request.form:
                    num_steps = int(request.form['num_steps']) 
                    #Register the number of steps, and set the correct status
                    wes_run.num_steps = num_steps
                    wes_run.run_status = "RUNNING"
                    wes_run.instance_status = "STARTED"
                    session.commit()
                    return wes_run.as_dict(), 201
                elif 'step_count' in request.form:
                    #NOTE: we auto increment step_count, assume step_count=1
                    #with db_manager.ManagedSession() as session:
                    #LOW-level operations just below ALSO WORKS--needs session.commit()
                    #session.query(WesRun).\
                    #    filter(WesRun.instance_name == instance_name).update({'step_count': result.step_count})
                    wes_run.step_count += 1
                    session.commit()
                    return wes_run.as_dict(), 201
                elif 'status' in request.form:
                    #Get either status = "COMPLETE" or "ERROR"
                    status = request.form['status']
                    if status == "COMPLETE" or status == "ERROR":
                        wes_run.run_status = status
                        wes_run.instance_status = "STOPPED"
                        #stop instance here
                        print("Stopping instance %s" % wes_run.instance_name)
                        instance.stop(_compute, wes_run.instance_name, _project, wes_run.zone)
                        session.commit()
                    return wes_run.as_dict(), 201
                else:
                    #malformed request
                    abort(400, message="UPDATE: missing parameters num_steps, step_count, or status")
            else:
                abort(404, message="UPDATE: instance %s does not exists in db" % instance_name)
            
    def get(self, instance_name):
        result = WesRun.query.filter_by(instance_name=instance_name).first()
        if result:
            return result.as_dict()
        else:
            abort(404, message="UPDATE: instance %s does not exists in db" % instance_name)


api.add_resource(Update, "/update/<string:instance_name>")

###############################################################################
# END WEB API with FLASK
###############################################################################

def cleanInstanceName(s):
    """cleans a string to make it a valid instance name"""
    #need to replace '.' with '-', b/c '.' is invalid char
    #also make it all lowercase
    return s.replace(".","-").lower()

class WesRun(db.Model):
    #instance specific cols
    id = db.Column(db.Integer, primary_key=True)
    instance_name = db.Column(db.String(100), nullable=False, unique=True)
    cores = db.Column(db.Integer, nullable=False, default=0)
    config_file = db.Column(db.String(100), nullable=False)
    zone = db.Column(db.String(100), nullable=False)
    
    #Status info
    instance_status = db.Column(db.String(100), nullable=False, default="STOPPED")  #{STOPPED, STARTED}
    run_status = db.Column(db.String(100), nullable=False, default="QUEUED") #{QUEUED, INITIALIZING, RUNNING, COMPLETED, ERROR}
    num_steps = db.Column(db.Integer, nullable=False, default=1)  #total number of wes steps
    step_count = db.Column(db.Integer, nullable=False, default=0)  #how many steps have completed

    #LEN TODO:
    #run_start_time = db.Column(db.DateTime, nullable=True) #when the run was started--on num_steps msg
    #last_checkin = db.Column(db.DateTime, nullable=True) #last message from instance, i.e. num_steps or step_count
    #stop_time = db.Column(db.DateTime, nullable=True) #if status mssage of COMPLETE or ERROR recieved, we stop the clock on the run
    
    def __init__(self, tumor_cimac_id, normal_cimac_id, google_bucket_path, tumor_fastq_path_pair1, tumor_fastq_path_pair2, normal_fastq_path_pair1, normal_fastq_path_pair2, rna_bam_file, rna_expression_file, cimac_center, cores, disk_size, wes_commit, image, wes_ref_snapshot, somatic_caller, trim_soft_clip, tumor_only, zone):
    #def __init__(self, **kwargs):
        """NOTE: given a dictionary representation of the run information
        from parseConfig_xlsx tries to wrap it into a formal class obj"""

        #instance specifications
        self.run_name = tumor_cimac_id
        self.instance_name = "wes-auto-" + cleanInstanceName(self.run_name)
        self.cores = cores
        self.disk_size = disk_size
        self.wes_commit = wes_commit
        self.image = image
        self.wes_ref_snapshot = wes_ref_snapshot

        #run info
        self.tumor_cimac_id = tumor_cimac_id
        self.normal_cimac_id = normal_cimac_id
        self.google_bucket_path = google_bucket_path
        
        self.cimac_center = cimac_center
        self.somatic_caller = somatic_caller
        self.trim_soft_clip = trim_soft_clip
        self.tumor_only = tumor_only
        self.zone = zone

        #sample info
        if tumor_fastq_path_pair1:
            #IS it paired end fastq or if pair2 is missing probably bam
            self.tumor_fastq_path = [tumor_fastq_path_pair1, tumor_fastq_path_pair2] if tumor_fastq_path_pair2 else [tumor_fastq_path_pair1]
        else:
            #ERROR!--what should we do--but exit
            print("WES run for %s could not be created b/c missing tumor path" % tumor_cimac_id)
            sys.exit(-1)
            
        if normal_fastq_path_pair1: #only checks that the first pair exists
            self.normal_fastq_path = [normal_fastq_path_pair1, normal_fastq_path_pair2] if normal_fastq_path_pair2 else [normal_fastq_path_pair1]
        else:
            self.normal_fastq_path = []


        #regular tumor-normal run
        if not self.tumor_only:
            self.samples = {self.tumor_cimac_id: self.tumor_fastq_path,
                            self.normal_cimac_id: self.normal_fastq_path}
        else:
            self.samples = {self.tumor_cimac_id: self.tumor_fastq_path}
        
        #metasheet info
        if not self.tumor_only:
            self.metasheet = {self.run_name: {'tumor': self.tumor_cimac_id,
                                              'normal': self.normal_cimac_id}}
        else:
            self.metasheet = {self.run_name: {'tumor': self.tumor_cimac_id,
                                              'normal': ''}}

        #RNA:
        if rna_bam_file and rna_expression_file:
            self.rna = {self.tumor_cimac_id: {'bam_file': rna_bam_file,
                                              'expression_file': rna_expression_file}}
        else:
            self.rna = None
            

        #TRY to make a wes_automator config file, which will set the
        #self.config_file
        self.makeConfig("%s.yaml" % self.run_name)

    def makeConfig(self, output_path):
        """Tries to generate a wes_automator config file and write to the 
        output path

        NOTE: everything needs to be 'quoted' except: cores, disk_size,
        trim_soft_clip
        """
        yaml = YAML()
        config_f = open(_wes_automator_template)
        config = yaml.load(config_f)
        config_f.close()

        #fill in the values-
        config['instance_name'] = SingleQuotedScalarString(cleanInstanceName(self.run_name))
        config['cores'] = self.cores
        config['disk_size'] = self.disk_size
        config['google_bucket_path'] = SingleQuotedScalarString(self.google_bucket_path)
        config['wes_commit'] = SingleQuotedScalarString(self.wes_commit)
        config['image'] = SingleQuotedScalarString(self.image)
        config['wes_ref_snapshot'] = SingleQuotedScalarString(self.wes_ref_snapshot)
        config['somatic_caller'] = SingleQuotedScalarString(self.somatic_caller)
        config['cimac_center'] = SingleQuotedScalarString(self.cimac_center)
        config['trim_soft_clip'] = self.trim_soft_clip
        config['tumor_only'] = self.tumor_only
        config['zone'] = self.zone

        config['samples'] = self.samples
        config['metasheet'] = self.metasheet
        if self.rna:
            config['rna'] = self.rna

        #output
        print("writing %s" % output_path)
        out = open(output_path, "w")
        yaml.dump(config, out)
        out.close()
        self.config_file = output_path

    def __repr__(self):
        #return str(self.__dict__)
        return "%s\t%s\t%s/%s" % (self.instance_name, self.run_status, str(self.step_count), str(self.num_steps))

    #serialize the object to json-
    #ref: https://stackoverflow.com/questions/5022066/how-to-serialize-sqlalchemy-result-to-json
    def as_dict(self):
       return {c.name: getattr(self, c.name) for c in self.__table__.columns}

def parseConfig_xlsx(xlsx_file):
    """parsese the xlsx config file and returns a list of dictionaries where
    each dictionary captures the information for each row"""
    #try to load the workbook:
    wb = load_workbook(xlsx_file)
    sheet = wb.active
    
    #load the data into a list of dictionaries
    cols = []
    rows = []
    for (i,r) in enumerate(sheet.rows):
        if i == 0:
            #the first row defines the columns
            cols = [c.value for c in r]
        else:
            tmp = {}
            for (key, cell) in zip(cols, r):
                tmp.__setitem__(key, cell.value)
            rows.append(tmp)
    #print(rows)
    return rows

#make _wes_runs global in scope so the web api can manipulate it
#_wes_runs = {}

def time_convert(sec):
    #ref: https://www.codespeedy.com/how-to-create-a-stopwatch-in-python/
    mins = sec // 60
    sec = math.floor(sec % 60)
    hours = mins // 60
    return("{0}:{1}:{2}".format(int(hours),int(mins),sec))

def allRunsCompleteOrErr(session):
    """Returns True iff all wes runs run_status are all COMPLETE or ERROR
    else False"""
    #Check whether there are any WesRuns that are not complete/err
    #if there are none, then you know all are complete or err
    not_compOrErr = session.query(WesRun).filter(and_(WesRun.run_status != 'COMPLETE', WesRun.run_status != 'ERROR'))
    return not_compOrErr.count() == 0

def getNextQueued(session):
    """Tries to return the next WesRun in the Queue, otherwise returns None"""
    next_run = session.query(WesRun).filter(WesRun.run_status == "QUEUED").order_by(WesRun.cores, WesRun.id).first()
    return next_run #Returns none is QUEUE is empty

def getRunningCores(session):
    """Returns the count of cores for running wes runs"""
    running = session.query(WesRun).filter(or_(WesRun.run_status == 'INITIALIZING', WesRun.run_status == 'RUNNING'))
    sum_cores = 0
    for run in running:
        sum_cores += run.cores
    return sum_cores

def printRunInfo():
    """Prints out the WesRun information"""
    with db_manager.ManagedSession() as session2:
        runs = session2.query(WesRun).all()
        for run in runs:
            print(run)

def main():
    usage = "USAGE: %prog -i [wes monitor config xlsx sheet] -u [gcp username] -k [gcp keyfile] -s [setup only--only sets up the wes run, does not actually run wes]"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-c", "--config", help="wes monitor config xlsx sheet")
    optparser.add_option("-u", "--user", help="username")
    optparser.add_option("-k", "--key_file", help="key file path")
    optparser.add_option("-s", "--setup_only", help="When this param is set, then wes_automator.py does everything EXCEPT run WES; this is helpful when you want to manually run wes sub-modules and not the entire pipeline. (default: False)", default=False, action="store_true")
    optparser.add_option("-p", "--port", help="port for flask server", default="5000")
    
    (options, args) = optparser.parse_args(sys.argv)
    if not options.config or not os.path.exists(options.config):
        print("Error: missing or non-existent wes monitor config xlsx file")
        optparser.print_help()
        sys.exit(-1)

    if (not options.user or not options.key_file):
        print("ERROR: missing user or google key path")
        optparser.print_help()
        sys.exit(-1)

    port = int(options.port)

    #Check if the db exists; create initial db if not
    if not os.path.exists(_db_filename):
        db.create_all()
        
    #parse the config
    wes_runs_configs = parseConfig_xlsx(options.config)
    #print(wes_runs_configs)
    
    #for each run, 1. add run info to db and 2. generate a config file
    for wes_run in wes_runs_configs:
        tmp = WesRun(**wes_run) #new WesRun object
        instance_name = "wes-auto-" + cleanInstanceName(tmp.run_name)
        result = WesRun.query.filter_by(instance_name=instance_name).first()
        #add it if it doesn't already exists
        if not result:
            tmp.instance_name = instance_name
            with db_manager.ManagedSession() as session:
                session.add(tmp)
                session.commit()

            #db.session.add(tmp)
            #db.session.commit()
            #print(tmp)

    start_time = time.time()
    num_cores_used = 0
    _max_cores = 64

    with db_manager.ManagedSession() as session:
        while not allRunsCompleteOrErr(session):
            #os.system('clear')
            next_run = getNextQueued(session)
            coresInUse = getRunningCores(session)
            coresAvail = _max_cores - coresInUse
            if next_run and coresAvail >= next_run.cores: #Available cores exceed next_run
                print("Starting run %s" % next_run.instance_name)
                wes_automator.run(next_run.config_file, options.user, options.key_file, options.setup_only)
                #TODO: save the log of each wes_automator run
                #cmd = "./wes_automator.py -c %s -u %s -k %s" % (next_run.config_file, options.user, options.key_file)
                #print(cmd)
                #subprocess.call(cmd.split(" "))
                #NOTE: we're run_status here so that we can account for the
                #cores-in use in the next loop of the while
                #We also do it in the num_steps msg  #it's ok to be redundant
                next_run.run_status = "INITIALIZING"
                session.commit()
                
            #print elapsed time
            now = time.time()
            time_msg = "Elapsed time %s" % time_convert(now - start_time)
            coresAvail_msg = "Cores avail %s" % coresAvail
            coresInUse_msg = "Cores in use %s" % coresInUse
            print("\t".join([time_msg, coresAvail_msg, coresInUse_msg]))
            #print the runs
            printRunInfo()
            sleep(2) #refresh every 20secs

    print("All runs complete\n\n")
    #print runs one last time
    printRunInfo()

def runFlask():
    app.run(debug=True, use_reloader=False, port=5000, host='0.0.0.0')

if __name__=='__main__':
    #Method to run Flask in a separate thread so we aren't blocking
    #ref https://raspberrypi.stackexchange.com/questions/113671/python-run-flask-webserver-parallel-from-main-code
    #But this doesn't work b/c sqlalchemy is not thread-safe!
    #ref: https://copdips.com/2019/05/using-python-sqlalchemy-session-in-multithreading.html
    #Run main
    t1 = threading.Thread(target=main).start()
    #Run Flask
    t2 = threading.Thread(target=runFlask).start()
