#!/usr/bin/env python
import os
import sys
import time
import subprocess
from optparse import OptionParser
from string import Template

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

    #GET the instance
    instance_name = "-".join(['wes-auto', config['instance_name']])
    compute = googleapiclient.discovery.build('compute', 'v1')
    response = instance.get_instance_from_name(compute, instance_name, _project, _zone)
    
    #SETUP ssh connection
    ip_addr = response['networkInterfaces'][0]['accessConfigs'][0]['natIP']
    ssh_conn = ssh(ip_addr, options.user, options.key_file)
    
    #ISSUE cmd
    (status, stdin, stderr) = ssh_conn.sendCommand("/home/taing/utils/wes_automator_tx.sh")
    if stderr:
        print(stderr)

    print("Transfer initiated.  please check instance @ %s" ip_addr)

if __name__=='__main__':
    main()
