import requests
import sys
import os
INSTANCE_NAME="wes-auto-c3rxbpckm-01"
#INSTANCE_NAME=os. environ. get("HOSTNAME") # CORRECT LINE!!
#SERVER_IP_PORT="10.142.0.77:5000"  #WILL BET TAKEN FROM A VARIABLE SET IN WES_AUTOMATOR
SERVER_IP_PORT="wes-automator-len:5000"  #WILL BET TAKEN FROM A VARIABLE SET IN WES_AUTOMATOR

def log_handler(msg):
    level = msg["level"]
        
    #RUNS AT RUN START
    if level == "run_info":
        #get total jobs (does it have msg["tota"] or will I have to parse text?
        #send total jobs to api
        print(msg)
        #Messages are a string with \n ans \t characters that create the table
        # ex. 'Job counts:\n\tcount\tjobs\n\t1\tall\n\t1\tstep1\n\t1\tstep2\n\t1\tstep3\n\t4'
        # if we split on \n we get lines
        # if we then split on \t we get columns
        # the last column has only one value, the total jobs
        #rows = msg["msg"].split("\n")
        #print("rows:", rows)
        #total = msg["msg"][-1]
        total= msg["msg"].split("\t")[-1] 
        print("we have %s total jobs" % total)
        #r = requests.put("http://%s/update/%s?num_steps=%s" % (SERVER_IP_PORT,INSTANCE_NAME,total))
        #print("http://%s/update/%s?num_steps=%s" % (SERVER_IP_PORT,INSTANCE_NAME,total))
        #print(r)
        print("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME))
        r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), params={'num_steps':total})
        #r=requests.get("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME))
        print(r)
        print(r.content)

        
    #RUNS WHEN JOBS COMPLETE, INCLUDING RUN COMPLETION 
    elif level == "progress":
        #self.job_count += 1
        step_count = msg["done"]
        if step_count == msg["total"]:
            print("we did it!")
            r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), params={'status':"COMPLETE"})
        else:
            print("still going")
            print(msg["done"])
            r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), params={'step_count':step_count})
            #should we keep a tally of which specific jobs have completed?

    elif level == "error":
        #extract which rule has caused the error
        #relay info to wes monitor
        print("uh oh! we have an error!")
        r=requests.put("http://%s/update/%s" % (SERVER_IP_PORT,INSTANCE_NAME), params={'status':"ERROR"})

    #elif level == "job_error": # how is this different than above??

    #elif level="job_info": # we can use msg["jobid"] and the timestamp to track runtime of individual jobs
