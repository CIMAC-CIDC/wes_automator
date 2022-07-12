#!/usr/bin/env python
"""Len Taing 2022 (TGBTG)
WES Monitor Viewer: Program to view wes monitor status

NOTE: The best practice for wes_monitor.py is to nohup the program so that it
continues to run even in the event of a disconnection

WES Monitor Viewer is a helper program that presents the user with an (almost)
real-time status of the wes runs
"""

import os
import sys
import time
from time import sleep
import math
from optparse import OptionParser
import wes_monitor

import db_manager

def main():
    usage = "USAGE: %prog -d [sqlite db filename]"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-d", "--db", help="sqlite db filename, default: wes_monitor.sqlite.db", default="wes_monitor.sqlite.db")
    (options, args) = optparser.parse_args(sys.argv)

    #SETUP db connection
    db_manager.init_engine('sqlite:///%s' % options.db)
    db_manager.init_session_factory()

    start_time = time.time()
    _max_cores = 320
    _sleep_time = 2 #sec
    with db_manager.ManagedSession() as session:
        while not wes_monitor.allRunsCompleteOrErr(session):
            os.system('clear')
            coresInUse = wes_monitor.getRunningCores(session)
            coresAvail = _max_cores - coresInUse
            
            #print elapsed time
            now = time.time()
            time_msg = "Elapsed time %s" % wes_monitor.time_convert(now - start_time)
            coresAvail_msg = "Cores avail %s" % coresAvail
            coresInUse_msg = "Cores in use %s" % coresInUse
            print("\t".join([time_msg, coresAvail_msg, coresInUse_msg]))
            #print the runs
            wes_monitor.printRunInfo()
            sleep(_sleep_time) #refresh every 20secs
            
if __name__=='__main__':
    main()
