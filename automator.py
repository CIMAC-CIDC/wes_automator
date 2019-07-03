#!/usr/bin/env python
import os
import sys
import time
import subprocess
from optparse import OptionParser

import googleapiclient.discovery

import paramiko
from paramiko import client

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

def createInstanceDisk(compute, instance_config, disk_config, ssh_config, project, zone):
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

    #attach disk to instance
    print("Attaching disk...")
    response = disk.attach_disk(compute, instance_config['name'], 
                                disk_config['name'], project, zone)

    #try to establish ssh connection:
    # wait 30 secs
    print("Establishing connection...")
    time.sleep(30)
    connection = ssh(ip_addr, ssh_config['user'], ssh_config['key'])
    #TEST connection
    #(status, stdin, stderr) = connection.sendCommand("ls /mnt")

    return (instanceId, ip_addr, connection)

def main():
    compute = googleapiclient.discovery.build('compute', 'v1')
    #TODO: update this!
    usage = "USAGE: %prog -n [instance name] -t [instance type (n1-highmem-96) -p [project (cidc-biofx)] -z [zone (us-east-1b]"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-n", "--instance_name", help="instance name")
    optparser.add_option("-i", "--image", help="image family")
    optparser.add_option("-m", "--machine_type", default="n1-highmem-96", help="machine_type")
    optparser.add_option("-p", "--project", default="cidc-biofx", help="google project")
    optparser.add_option("-s", "--service_account", default="biofxvm@cidc-biofx.iam.gserviceaccount.com", help="service account (biofxvm@cidc-biofx.iam.gserviceaccount.com)")
    optparser.add_option("-z", "--zone", default="us-east1-b", help="zone")

    optparser.add_option("-d", "--disk_name", help="disk name")
    optparser.add_option("-g", "--disk_size", help="disk size in Gb ")
    optparser.add_option("-u", "--user", help="username")
    optparser.add_option("-k", "--key_file", help="key file path")

    optparser.add_option("-c", "--commit_str", default="", help="wes commit string/branch")

    (options, args) = optparser.parse_args(sys.argv)

    if not options.instance_name:
        print("ERROR: an unique instance name is required")
        optparser.print_help()
        sys.exit(-1)
    elif not options.image:
        print("ERROR: an image family, e.g. 'wes' 'cidc_chips' is required")
        optparser.print_help()
        sys.exit(-1)

    if not options.disk_name or not options.disk_size:
        print("ERROR: a disk name and a disk size is required")
        optparser.print_help()
        sys.exit(-1)

    if (not options.user or not options.key_file):
        print("ERROR: missing user or google key path")
        optparser.print_help()
        sys.exit(-1)


    instance_config= {'name': options.instance_name, 
                      'image': options.image, 
                      'machine_type': options.machine_type, 
                      'serviceAcct': options.service_account}

    disk_config= {'name': options.disk_name, 
                  'size': options.disk_size}

    ssh_config= {'user': options.user, 
                 'key': options.key_file}

    (instanceId, ip_addr, ssh_conn) = createInstanceDisk(compute, 
                                                         instance_config, 
                                                         disk_config, 
                                                         ssh_config, 
                                                         options.project, 
                                                         options.zone)
    print("Successfully created instance %s" % instance_config['name'])
    print("{instanceId: %s, ip_addr: %s, disk: %s}" % (instanceId, ip_addr, disk_config['name']))

    #SETUP the instance, disk, and wes directory
    (status, stdin, stderr) = ssh_conn.sendCommand("/home/taing/utils/wes_automator.sh %s %s" % (options.user, options.commit_str))
    if stderr:
        print(stderr)

    # #setup wes dir
    # (status, stdin, stderr) = ssh_conn.sendCommand("/home/taing/utils/setup01_newWESproj.sh %s" % options.commit_str)
    # if stderr:
    #     print(stderr)

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
