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


def cleanInstanceName(s):
    """cleans a string to make it a valid instance name"""
    #need to replace '.' with '-', b/c '.' is invalid char
    #also make it all lowercase
    return s.replace(".","-").lower()

class WesRun():
    #use a template to build run-specific configs
    wes_automator_template = "config.automator.template.yaml"
    
    def __init__(self, tumor_cimac_id, normal_cimac_id, google_bucket_path, tumor_fastq_path_pair1, tumor_fastq_path_pair2, normal_fastq_path_pair1, normal_fastq_path_pair2, rna_bam_file, rna_expression_file, cimac_center, cores, disk_size, wes_commit, image, wes_ref_snapshot, somatic_caller, trim_soft_clip, tumor_only):
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
            
        #Status info
        self.instance_status = "STOPPED" #{STOPPED, STARTED}
        self.run_status = "QUEUED" #{QUEUED, RUNNING, COMPLETED, ERROR}
        self.num_steps = None #total number of wes steps
        self.step_count = 0 #how many steps have completed

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
        config_f = open(self.wes_automator_template)
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
        return str(self.__dict__)


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

def main():
    usage = "USAGE: %prog -i [wes monitor config xlsx sheet] -u [gcp username] -k [gcp keyfile] -s [setup only--only sets up the wes run, does not actually run wes]"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-c", "--config", help="wes monitor config xlsx sheet")
    optparser.add_option("-u", "--user", help="username")
    optparser.add_option("-k", "--key_file", help="key file path")
    optparser.add_option("-s", "--setup_only", help="When this param is set, then wes_automator.py does everything EXCEPT run WES; this is helpful when you want to manually run wes sub-modules and not the entire pipeline. (default: False)", default=False, action="store_true")
    
    (options, args) = optparser.parse_args(sys.argv)
    if not options.config or not os.path.exists(options.config):
        print("Error: missing or non-existent wes monitor config xlsx file")
        optparser.print_help()
        sys.exit(-1)

    if (not options.user or not options.key_file):
        print("ERROR: missing user or google key path")
        optparser.print_help()
        sys.exit(-1)

    #parse the config
    wes_runs_configs = parseConfig_xlsx(options.config)
    #print(wes_runs_configs)
    
    #for each run, generate a config file; return a dictionary of wes_instance
    #names (including the wes-auto prefix) and config files
    wes_runs = {}
    for wes_run in wes_runs_configs:
        tmp = WesRun(**wes_run)
        wes_runs[tmp.instance_name] = tmp
    #print(wes_runs)

    #RUN loop here
    wes_automator.run(wes_runs['wes-auto-c3rxbpckm-01'].config_file, options.user, options.key_file, options.setup_only)
    
if __name__=='__main__':
    main()

