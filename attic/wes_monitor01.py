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
#import threading
from optparse import OptionParser

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import SingleQuotedScalarString

import wes_automator
import instance
import disk

from openpyxl.reader.excel import load_workbook

###############################################################################
# WEB API with FLASK
###############################################################################
from flask import Flask
from flask_restful import Api, Resource, request, abort #reqparse, abort, fields, marshal_with
from flask_sqlalchemy import SQLAlchemy

#GLOBALS
app = Flask(__name__)
api = Api(app)
_db_filename="wes_monitor.sqlite.db"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///%s' % _db_filename
db = SQLAlchemy(app)

#use a template to build run-specific configs
_wes_automator_template = "config.automator.template.yaml"

#define a /register/<instance_name>/?num_steps=
class Update(Resource):
    def put(self, instance_name):
        result = WesRun.query.filter_by(instance_name=instance_name).first()
        if result:
            if 'num_steps' in request.form:
                num_steps = int(request.form['num_steps'])
                #Register the number of steps
                result.num_steps = num_steps
                db.session.commit()
                #need to serialize the WesRun object to json
                #print(result)
                return result.as_dict(), 201
            elif 'step_count' in request.form:
                #NOTE: we auto increment step_count, assume step_count=1
                result.step_count +=1
                db.session.commit()
                return result.as_dict(), 201
            else:
                #malformed request
                abort(400, message="UPDATE: missing parameter num_steps")
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
    #config_file = db.Column(db.String(100), nullable=False)
    
    #Status info
    instance_status = db.Column(db.String(100), nullable=False, default="STOPPED")  #{STOPPED, STARTED}
    run_status = db.Column(db.String(100), nullable=False, default="QUEUED") #{QUEUED, RUNNING, COMPLETED, ERROR}
    num_steps = db.Column(db.Integer, nullable=False, default=1)  #total number of wes steps
    step_count = db.Column(db.Integer, nullable=False, default=0)  #how many steps have completed
    
    def __init__(self, tumor_cimac_id, normal_cimac_id, google_bucket_path, tumor_fastq_path_pair1, tumor_fastq_path_pair2, normal_fastq_path_pair1, normal_fastq_path_pair2, rna_bam_file, rna_expression_file, cimac_center, cores, disk_size, wes_commit, image, wes_ref_snapshot, somatic_caller, trim_soft_clip, tumor_only):
    #def __init__(self, **kwargs):
        """NOTE: given a dictionary representation of the run information
        from parseConfig_xlsx tries to wrap it into a formal class obj"""

        #instance specifications
        self.run_name = tumor_cimac_id
        #self.instance_name = "wes-auto-" + cleanInstanceName(self.run_name)
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
            db.session.add(tmp)
            db.session.commit()
            #print(tmp)

    #Start up the REST API--hopefully it doesn't block
    #ref:
    #app.run(debug=True) #blocking call
    #threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)).start()


    print("foo")

    #RUN loop here
    #wes_automator.run(wes_runs['wes-auto-c3rxbpckm-01'].config_file, options.user, options.key_file, options.setup_only)
    
if __name__=='__main__':
    main()

