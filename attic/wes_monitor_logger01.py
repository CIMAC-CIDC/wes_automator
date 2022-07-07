import requests
import sys
import os
#INSTANCE_NAME="wes-auto-c3rxbpckm-01"
INSTANCE_NAME="wes-auto-c4rxbpckm-01"
#INSTANCE_NAME=os. environ. get("HOSTNAME") # CORRECT LINE!!
SERVER_IP_PORT="10.142.0.77:5000"  #WILL BET TAKEN FROM A VARIABLE SET IN WES_AUTOMATOR
#SERVER_IP_PORT="wes-automator-len:5000"  #WILL BET TAKEN FROM A VARIABLE SET IN WES_AUTOMATOR

def log_handler(msg):
    level = msg["level"]
        
    #RUNS AT RUN START
    if level == "run_info":
        #get total jobs (does it have msg["tota"] or will I have to parse text?
        #send total jobs to api
        #Messages are a string with \n ans \t characters that create the table
        # ex. 'Job counts:\n\tcount\tjobs\n\t1\tall\n\t1\tstep1\n\t1\tstep2\n\t1\tstep3\n\t4'
        # if we split on \n we get lines
        # if we then split on \t we get columns
        # the last column has only one value, the total jobs
        #LAST line = "total   6     1     1"
        #want to take 2nd col, i.e. 6
        #print("\n\n%s\n\n" % msg['msg'].strip().split("\n")[-1].split()[1])
        total= msg['msg'].strip().split("\n")[-1].split()[1]
        #NOTE: need to -1 from the total to correct for the "all" job
        total = str(int(total) - 1)
        print("we have %s total jobs" % total)
        #print("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME))
        try:
            r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), params={'num_steps':total}, data={'num_steps':total})
            #print(r.content)
        except:
            print("WES Monitor server down %s" % SERVER_IP_PORT)

        
    #RUNS WHEN JOBS COMPLETE, INCLUDING RUN COMPLETION 
    elif level == "progress":
        #self.job_count += 1
        step_count = msg["done"]
        if step_count == msg["total"]:
            print("we did it!")
            try:
                r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), data={'status':"COMPLETE"})
            except:
                print("WES Monitor server down %s" % SERVER_IP_PORT)
        else:
            print("still going")
            print(msg["done"])
            try:
                r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), data={'step_count':step_count})
            except:
                print("WES Monitor server down %s" % SERVER_IP_PORT) 
            #should we keep a tally of which specific jobs have completed?

    elif level == "error":
        #extract which rule has caused the error
        #relay info to wes monitor
        print("uh oh! we have an error!")
        try:
            r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), data={'status':"ERROR"})
        except:
            print("WES Monitor server down %s" % SERVER_IP_PORT) 

    #elif level == "job_error": # how is this different than above??

    #elif level="job_info": # we can use msg["jobid"] and the timestamp to track runtime of individual jobs
