#!/usr/bin/env python
import os
import sys
import time
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

    def sendCommand(self, command):
        # Check if connection is made previously
        #ref: https://www.programcreek.com/python/example/7495/paramiko.SSHException
        #example 3
        if(self.client):
            status = 0
            try:
                t = self.client.exec_command(command)
            except paramiko.SSHException:
                status=1

            #NOTE: reverting to python2 method of utf-8 conversion
            std_out = unicode(t[1].read(), "utf-8") #str(t[1].read(), "utf-8")
            std_err = unicode(t[2].read(), "utf-8") #str(t[2].read(), "utf-8")
            t[0].close()
            t[1].close()
            t[2].close()
            return (status, std_out, std_err)

def createInstanceDisk(compute, instance_config, disk_config, ssh_config, project, zone, disk_auto_del=True):
    #create a new instance
    print("Creating instance...")
    response = instance.create(compute, instance_config['name'], 
                               instance_config['image'], 
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

    #SET the auto-delete flag for the newly created disk
    print("Setting disk auto-delete flag for disk %s" % disk_config['name'])
    disk_dev_name = instance.get_disk_device_name(compute, instance_config['name'], project, zone, disk_config['name'])
    if disk_dev_name:
        print("Found disk %s attached as %s" % (disk_config['name'], disk_dev_name))
        response = instance.set_disk_auto_delete(compute, instance_config['name'], project, zone, disk_dev_name, disk_auto_del)
    else:
        print("WARNING: Setting of disk auto-delete flag failed")
    print(response.to_json())

    #try to establish ssh connection:
    # wait 30 secs
    print("Establishing connection...")
    time.sleep(30)
    connection = ssh(ip_addr, ssh_config['user'], ssh_config['key'])
    #TEST connection
    #(status, stdin, stderr) = connection.sendCommand("ls /mnt")

    return (instanceId, ip_addr, connection)

def main():
    usage = "USAGE: %prog -c [wes_automator config yaml] -u [google account username, e.g. taing] -k [google account key path, i.e. ~/.ssh/google_cloud_enging"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-c", "--config", help="instance name")
    optparser.add_option("-u", "--user", help="username")
    optparser.add_option("-k", "--key_file", help="key file path")
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

    #SET DEFAULTS
    _commit_str = "" if not "wes_commit" in config else config['wes_commit']
    _image = "wes" if not "image" in config else config['image']
    _project = "cidc-biofx" if not "project" in config else config['project']
    _service_account = "biofxvm@cidc-biofx.iam.gserviceaccount.com"
    _zone = "us-east1-b" if not "zone" in config else config['zone']
    #dictionary of machine types based on cores
    _machine_types = {'16': 'n1-highmem-16',
                      '32': 'n1-highmem-32',
                      '64': 'n1-highmem-64',
                      '96': 'n1-highmem-96'}

    #AUTO append "wes_auto_" to instance name
    instance_name = "-".join(['wes-auto', config['instance_name']])
    #AUTO name attached disk
    disk_name = "-".join([instance_name, 'disk'])
    disk_size = config['disk_size']

    #SET machine type (default to n1-standard-8 if the core count is undefined
    machine_type = "n1-standard-8"
    if 'cores' in config and str(config['cores']) in _machine_types:
        machine_type = _machine_types[str(config['cores'])]

    instance_config= {'name': instance_name, 
                      'image': _image, 
                      'machine_type': machine_type, 
                      'serviceAcct': _service_account}

    disk_config= {'name': disk_name, 
                  'size': disk_size}

    ssh_config= {'user': options.user, 
                 'key': options.key_file}

    #print(instance_config)
    #print(disk_config)
    #print(ssh_config)
    compute = googleapiclient.discovery.build('compute', 'v1')
    (instanceId, ip_addr, ssh_conn) = createInstanceDisk(compute, 
                                                         instance_config, 
                                                         disk_config, 
                                                         ssh_config, 
                                                         _project, 
                                                         _zone)

    print("Successfully created instance %s" % instance_config['name'])
    print("{instanceId: %s, ip_addr: %s, disk: %s}" % (instanceId, ip_addr, disk_config['name']))

    #SETUP the instance, disk, and wes directory
    print("Setting up the attached disk...")
    (status, stdin, stderr) = ssh_conn.sendCommand("/home/taing/utils/wes_automator.sh %s %s" % (options.user, _commit_str))
    if stderr:
        print(stderr)

    #setup config and metahseet
    #UPLOAD for now
    #cmd = 'scp -i %s %s %s@%s:%s' % (options.key_file, "config.yaml",
    #                                 options.user, ip_addr,
    #                                 "/mnt/ssd/wes/")
    #response = subprocess.check_output(cmd, hell=True).decode('utf-8')
    #cmd = 'scp -i %s %s %s@%s:%s' % (options.key_file, "metasheet.csv",
    #                                 options.user, ip_addr,
    #                                 "/mnt/ssd/wes/")
    #response = subprocess.check_output(cmd, hell=True).decode('utf-8')

    #download data
    # gs_path="gs://lens_bucket2/wes/speed_tests/mutsig/out.log"    
    # print("Downloading raw files...")
    # (status, stdin, stderr) = ssh_conn.sendCommand("/home/taing/utils/dnldBucket.sh %s %s" % (gs_path, "/mnt/ssd/wes/data"))
    # if stderr:
    #     print(stderr)

    #try dry run
if __name__=='__main__':
    main()
