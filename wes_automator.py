#!/usr/bin/env python
"""Len Taing 2019 (TGBTG)
WES automator script to automatically run WES on GCP
"""

import os
import sys
import time
import string
import random
import subprocess
from optparse import OptionParser

import googleapiclient.discovery

import paramiko
from paramiko import client

import ruamel.yaml

import instance
import disk
from instance import wait_for_operation

class ssh:
    client = None

    def __init__(self, address, username, key_filename):
        # Let the user know we're connecting to the server
        print("Connecting to server.")
        # Create a new SSH client
        self.client = client.SSHClient()
        # The following line is required if you want the script to be able to access a server that's not yet in the known_hosts file
        self.client.set_missing_host_key_policy(client.AutoAddPolicy())
        # Make the connection
        self.client.connect(address, username=username, key_filename=key_filename, look_for_keys=False)

    def sendCommand(self, command, timeout=None):
        # Check if connection is made previously
        #ref: https://www.programcreek.com/python/example/7495/paramiko.SSHException
        #example 3
        if(self.client):
            status = 0
            try:
                t = self.client.exec_command(command, timeout)
            except paramiko.SSHException:
                status=1

            #NOTE: reverting to python2 method of utf-8 conversion
            std_out = unicode(t[1].read(), "utf-8") #str(t[1].read(), "utf-8")
            std_err = unicode(t[2].read(), "utf-8") #str(t[2].read(), "utf-8")
            t[0].close()
            t[1].close()
            t[2].close()
            return (status, std_out, std_err)

def checkConfig_bucketPath(a_dict, invalid_bucket_paths):
    """Given a dictionary of {key: [list of google bucket paths], ...}
    OR a dictionary of dictionaries (of google paths {key: {foo: path, ..}..}
    Will check each of the bucket paths associated with the key and 
    if any are invalid, will add them to the invalid_bucket_path list
    NOTE: this was an inner loop in checkConfig which we're trying to reuse"""

    for sample in a_dict:
        for f in a_dict[sample]:
            if isinstance(a_dict[sample], list):
                ffile = f
            else: #dictionary
                ffile = a_dict[sample][f]

            cmd = [ "gsutil", "ls", ffile]
            print(" ".join(cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, 
                                    stderr=subprocess.PIPE)
            (out, error) = proc.communicate()
            if proc.returncode != 0:
                #print("Error %s:" % proc.returncode)
                #print(out)
                print(error)
                invalid_bucket_paths.append(f)

def checkConfig(wes_auto_config):
    """Does some basic checks on the config file
    INPUT config file parsed as a dictionary
    returns True if everything is ok
    otherwise exits!
    """
    required_fields = ["instance_name", "cores", "disk_size",
                       "google_bucket_path", "samples", "metasheet"]
    #optional_fields = ['wes_commit'] #not used below!!

    missing = []
    for f in required_fields:
        if not f in wes_auto_config or not wes_auto_config[f]:
            missing.append(f)

    #check if the sample fastq/bam files are valid
    invalid_bucket_paths = []
    print("Checking the sample file paths...")
    checkConfig_bucketPath(wes_auto_config['samples'], invalid_bucket_paths)

    rna = wes_auto_config.get('rna', None)
    if rna:
        print("Checking the RNA-seq file paths...")
        checkConfig_bucketPath(rna, invalid_bucket_paths)

    if invalid_bucket_paths:
        print("Some sample file bucket files are invalid or do not exist, please correct this.")
        for f in invalid_bucket_paths:
            print(f)
        sys.exit()

    #NOTE: can add additional checks like google_bucket_path is in the form
    #of 'gs://...' etc.  but this is a start

    if missing:
        print("ERROR: Please define these required params in the automator config file:\n%s" % ", ".join(missing))
        sys.exit()
    else:
        return True

def createInstanceDisk(compute, instance_config, disk_config, wes_ref_snapshot, ssh_config, project, zone, disk_auto_del=True):
    #create a new instance
    print("Creating instance...")
    response = instance.create(compute, instance_config['name'],
                               instance_config['image_name'],
                               instance_config['image_family'],
                               instance_config['machine_type'],
                               project,
                               instance_config['serviceAcct'], 
                               zone)
    instanceLink = response['targetLink']
    instanceId = response['targetId']
    print(instanceLink, instanceId)
    #try to get the instance ip address
    ip_addr = instance.get_instance_ip(compute, instanceId, project, zone)
    
    #create a new disk
    print("Creating disk...")
    response = disk.create(compute, disk_config['name'], disk_config['size'], 
                           project, zone)
    #print(response)

    #attach disk to instance
    print("Attaching disk...")
    response = disk.attach_disk(compute, instance_config['name'], 
                                disk_config['name'], project, zone)
    #print(response)

    #CREATE REF DISK from snapshot given
    print("Creating reference disk...")
    ref_disk_name = "-".join([instance_config['name'], 'ref-disk'])
    response = disk.createFromSnapshot(compute, ref_disk_name, 
                                       wes_ref_snapshot, project, zone)
    #print(response)

    #attach disk to instance
    print("Attaching reference disk...")
    response = disk.attach_disk(compute, instance_config['name'], 
                                ref_disk_name, project, zone)

    #try to establish ssh connection:
    # wait 30 secs
    print("Establishing connection...")
    #time.sleep(60)
    connection = ssh(ip_addr, ssh_config['user'], ssh_config['key'])
    #TEST connection
    #(status, stdin, stderr) = connection.sendCommand("ls /mnt")

    #SET the auto-delete flag for the newly created disks
    print("Setting disk auto-delete flag for disk %s" % disk_config['name'])
    #NOTE: using the instance.set_disk_auto_delete fn doesn't work
    #TRY manual call
    #NOTE: the attached disk is always going to be persistent-disk-1
    cmd = [ "gcloud", "compute", "instances", "set-disk-auto-delete", instance_config['name'], "--device-name", "persistent-disk-1", "--zone", zone]
    print(" ".join(cmd))
    proc = subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    (out, error) = proc.communicate()
    if proc.returncode != 0:
        print("Error %s:" % proc.returncode)
        #print(out)
        print(error)

    #NOTE: ref disk is sdc which is peristent-disk-2
    cmd = [ "gcloud", "compute", "instances", "set-disk-auto-delete", instance_config['name'], "--device-name", "persistent-disk-2", "--zone", zone]
    print(" ".join(cmd))
    proc = subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    (out, error) = proc.communicate()
    if proc.returncode != 0:
        print("Error %s:" % proc.returncode)
        #print(out)
        print(error)

    return (instanceId, ip_addr, connection)

#NOTE: lots of redundancy betwwen this and the local version, but for now
#saving a complete working copy
def transferRawFiles_remote(samples, bucket_path):
    """Transfers the samples from their source location to the wes project 
    location (a google bucket)
    RETIRNS: a dictionary of samples with their new data paths (which are 
    relative to the wes project location i.e. google bucket path
    """
    # PUT the files in {bucket_path}/data
    # and build up new sample dictionary (tmp)
    tmp = {}
    for sample in samples:
        for fq in samples[sample]:
            #add this to the samples dictionary
            if sample not in tmp:
                tmp[sample] = []
            # get the filename, e.g. XXX.fq.gz
            filename = fq.split("/")[-1]
            tmp[sample].append("data/%s" % filename)

            if bucket_path.endswith("/"):
                dst = "%sdata/" % bucket_path
            else:
                dst = "%s/data/" % bucket_path

            cmd = [ "gsutil", "-m", "cp", fq, dst]
            print(" ".join(cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, 
                                    stderr=subprocess.PIPE)
            (out, error) = proc.communicate()
            if proc.returncode != 0:
                print("Error %s:" % proc.returncode)
                #print(out)
                print(error)
    return tmp

def transferRawFiles_local(samples, ssh_conn, sub_dir, wes_dir='/mnt/ssd/wes'):
    """Goes through each FILE associated with each sample and issues
    a cmd from the instance to download the file to /mnt/ssd/wes/data

    RETURNS: a dictionary of samples with their new data paths

    NOTE: This function handles two types of data structures
    1. dictionary of lists: e.g. 'samples'-
       {sample: [google bucket file paths, ...], ...}
    2. dictionary of dictionaries which define google bucket paths, eg. 'rna'-
       {sample: {bam_file: <google bucket path>, expression_file: <path>}...}
    """

    tmp = {}
    for sample in samples:
        for fq in samples[sample]:
            #Check if this is dictionary of lists or dict of dicts
            if isinstance(samples[sample], list): #is a list
                #add this to the samples dictionary
                if sample not in tmp:
                    tmp[sample] = []

                # get the filename, e.g. XXX.fq.gz or XXX.bsm
                ffile = fq
                filename = ffile.split("/")[-1]
                tmp[sample].append(os.path.join(sub_dir, sample, filename))
            else: #dictionary
                #add this to the samples dictionary
                if sample not in tmp:
                    tmp[sample] = {}

                ffile = samples[sample][fq] #de-reference once more
                filename = ffile.split("/")[-1]
                #MAKE sure that the data structure remains a dictionary
                tmp[sample][fq] = os.path.join(sub_dir, sample, filename)


            #HARDCODED location of where the data files are expected--
            #no trailing /
            #dst = "/mnt/ssd/wes/data"
            dst = os.path.join(wes_dir, sub_dir, sample)

            #MAKE the data directory
            (status, stdin, stderr) = ssh_conn.sendCommand("mkdir -p %s" % dst)
            cmd = " ".join([ "gsutil", "-m", "cp", ffile, dst])
            print(cmd)
            (status, stdin, stderr) = ssh_conn.sendCommand(cmd)
            if stderr:
                print(stderr)
            
    return tmp

def main():
    usage = "USAGE: %prog -c [wes_automator config yaml] -u [google account username, e.g. taing] -k [google account key path, i.e. ~/.ssh/google_cloud_enging"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-c", "--config", help="instance name")
    optparser.add_option("-u", "--user", help="username")
    optparser.add_option("-k", "--key_file", help="key file path")
    optparser.add_option("-s", "--setup_only", help="When this param is set, then wes_automator.py does everything EXCEPT run WES; this is helpful when you want to manually run wes sub-modules and not the entire pipeline. (default: False)", default=False, action="store_true")
    (options, args) = optparser.parse_args(sys.argv)

    if not options.config or not os.path.exists(options.config):
        print("Error: missing or non-existent yaml configuration file")
        optparser.print_help()
        sys.exit(-1)

    if (not options.user or not options.key_file):
        print("ERROR: missing user or google key path")
        optparser.print_help()
        sys.exit(-1)

    # PARSE the yaml file
    config_f = open(options.config)
    config = ruamel.yaml.round_trip_load(config_f.read())
    config_f.close()

    #CHECK config
    checkConfig(config)

    #SET DEFAULTS
    _sentieon_path = config.get("sentieon_path", "/home/taing/sentieon/sentieon-genomics-201808.05/bin/")
    _commit_str = config.get('wes_commit', "")
    _somatic_caller = config.get('somatic_caller', 'tnscope')
    _cimac_center = config.get("cimac_center","broad")
    _trim_soft_clip = config.get("trim_soft_clip", False)
    _tumor_only = config.get("tumor_only", False)
    #NOTE: IF a specific GCP image is not set via config['image'], then
    #the default behavior is to get the latest wes image
    _image_name = config.get('image', '')
    _image_family = config.get('image_family', 'wes')
    _project = config.get("project", "cidc-biofx")
    _service_account = "biofxvm@cidc-biofx.iam.gserviceaccount.com"
    _zone = config.get("zone", "us-east1-b")
    #dictionary of machine types based on cores
    _machine_types = {'2': 'n2-standard-2',
                      '4': 'n2-standard-4',
                      '8': 'n2-standard-8',
                      '16': 'n2-standard-16',
                      '32': 'n2-standard-32',
                      '64': 'n2-standard-64',
                      '96': 'n2-standard-96'}

    #SHOULD I error check these?
    #AUTO append "wes_auto_" to instance name
    instance_name = "-".join(['wes-auto', config['instance_name']])
    #AUTO name attached disk
    disk_name = "-".join([instance_name, 'disk'])
    disk_size = config['disk_size']

    #SET machine type (default to n2-standard-8 if the core count is undefined)
    machine_type = "n2-standard-8"
    if 'cores' in config and str(config['cores']) in _machine_types:
        machine_type = _machine_types[str(config['cores'])]

    #The google bucket path is in the form of gs:// ...
    #The normal_bucket path is the google bucket path but without the gs://
    google_bucket_path = config['google_bucket_path']
    normal_bucket_path = google_bucket_path.replace("gs://","") #remove gsL//

    instance_config= {'name': instance_name, 
                      'image_name': _image_name,
                      'image_family': _image_family,
                      'machine_type': machine_type, 
                      'serviceAcct': _service_account}

    disk_config= {'name': disk_name, 
                  'size': disk_size}

    wes_ref_snapshot = config.get('wes_ref_snapshot', 'wes-ref-ver1-0')
    ssh_config= {'user': options.user, 
                 'key': options.key_file}

    #print(instance_config)
    #print(disk_config)
    #print(ssh_config)
    compute = googleapiclient.discovery.build('compute', 'v1')
    (instanceId, ip_addr, ssh_conn) = createInstanceDisk(compute, 
                                                         instance_config, 
                                                         disk_config, 
                                                         wes_ref_snapshot,
                                                         ssh_config, 
                                                         _project, 
                                                         _zone)

    print("Successfully created instance %s" % instance_config['name'])
    print("{instanceId: %s, ip_addr: %s, disk: %s}" % (instanceId, ip_addr, disk_config['name']))
#------------------------------------------------------------------------------
    #SETUP the instance, disk, and wes directory
    print("Setting up the attached disk...")
    cmd= "/home/taing/utils/wes_automator.sh %s %s" % (options.user, _commit_str)
    #print(cmd)
    (status, stdin, stderr) = ssh_conn.sendCommand(cmd)
    if stderr:
        print(stderr)
#------------------------------------------------------------------------------
    # transfer the data to the bucket directory
    print("Transferring raw files from the bucket...")
    samples = transferRawFiles_local(config['samples'], ssh_conn, 'data')

    rna = config.get('rna', None)
    if rna:
        print("Transferring RNA-seq files from the bucket...")
        rna = transferRawFiles_local(rna, ssh_conn, 'rna_data')

#------------------------------------------------------------------------------
    # Write a config (.config.yaml) and a meta (.metasheet.csv) locally
    # then upload it to the instance
    # CONFIG.yaml
    print("Setting up the config.yaml...")
    # parse the wes_config.yaml template
    #NOTE: using the local version of the config
    wes_config_f = open('wes_config.local.yaml')
    wes_config = ruamel.yaml.round_trip_load(wes_config_f.read())
    wes_config_f.close()
    
    # SET the config to the samples dictionary we built up
    wes_config['samples'] = samples
    if rna:
        wes_config['rna'] = rna
    # ADD somatic caller
    wes_config['somatic_caller'] = _somatic_caller
    # ADD cimac_center
    wes_config['cimac_center'] = _cimac_center
    # Add trim_soft_clip
    wes_config['trim_soft_clip'] = _trim_soft_clip
    # Add tumor_only
    wes_config['tumor_only'] = _tumor_only
    # Add sentieon path
    wes_config['sentieon_path'] = _sentieon_path
    ##set transfer path
    transfer_path = normal_bucket_path
    #check if transfer_path has gs:// in front
    if not transfer_path.startswith("gs://"):
        transfer_path = "gs://%s" % transfer_path

    if transfer_path.endswith("/"):
        wes_config['transfer_path'] = transfer_path
    else:
        wes_config['transfer_path'] = transfer_path + "/"
    ##print(wes_config)

    #WRITE this to hidden file .config.yaml
    print("Setting up the config and metasheet...")
    #prepend a random string to these files
    salt=''.join(random.choice(string.ascii_lowercase) for i in range(6))
    print("writing %s" % (".config.%s.yaml" % salt))
    out = open(".config.%s.yaml" % salt,"w")
    #NOTE: this writes the comments for the metasheet as well, but ignore it
    ruamel.yaml.round_trip_dump(wes_config, out)
    out.close()

    # METASHEET.csv
    # write the metasheet to .metasheet.csv
    print("writing %s" % (".metasheet.%s.csv" % salt))
    out = open(".metasheet.%s.csv" % salt,"w")
    out.write("RunName,Normal,Tumor\n")
    for run in config['metasheet']:
        normal = config['metasheet'][run].get('normal', '')
        tumor = config['metasheet'][run]['tumor']
        out.write("%s\n" % ','.join([run, normal, tumor]))
    out.close()
#------------------------------------------------------------------------------
    #UPLOAD .config.yaml and .metasheet.csv
    #NOTE: we are skip checking .ssh/known_hosts
    #really should make this a fn

    #upload wes automator config file as well
    wes_auto_config_f = options.config.split("/")[-1] #Take out config fname
    for f in [('config.yaml', ".config.%s.yaml" % salt),
              ('metasheet.csv', ".metasheet.%s.csv" % salt),
              (wes_auto_config_f, options.config)]:
        (basename, fname) = f
        cmd = ['scp', "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", '-i', options.key_file, "%s" % fname, "%s@%s:%s%s" % (options.user, ip_addr, "/mnt/ssd/wes/", basename)]
        print(" ".join(cmd))
        proc = subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        (out, error) = proc.communicate()
        if proc.returncode != 0:
            print("Error %s:" % proc.returncode)
            print(error)

    #RUN
    if not options.setup_only: 
        print("Running...")
        #NOTE: _project and _bucket_path are not needed for local runs
        (status, stdin, stderr) = ssh_conn.sendCommand("/home/taing/utils/wes_automator_run_local.sh %s %s %s" % (_project, normal_bucket_path, str(config['cores'])))
        if stderr:
            print(stderr)
    else:
        print("Setup only selected, please log into the instance to run WES manually")

    print("The instance %s is running at the following IP: %s" % (instance_name, ip_addr))
    print("please log into this instance and to check-in on the run")

if __name__=='__main__':
    main()
