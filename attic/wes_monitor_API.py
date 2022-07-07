#!/usr/bin/env python
"""Len Taing 2022 (TGBTG)
WES Monitor API: WEB API to update WES monitor runs built on Flask and
SQLAlchemy
"""
from wes_monitor import WesRun, app, db

###############################################################################
# WEB API with FLASK
###############################################################################
from flask import Flask
from flask_restful import Api, Resource, request, abort #reqparse, abort, fields, marshal_with
from flask_sqlalchemy import SQLAlchemy

#GLOBALS
#app = Flask(__name__)
api = Api(app)
#_db_filename="wes_monitor.sqlite.db"
#app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///%s' % _db_filename
#db = SQLAlchemy(app)

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
def main():
    usage = "USAGE: %prog -p [port, default 5000]"
    optparser = OptionParser(usage=usage)
    optparser.add_option("-p", "--port", help="port for flask server", default="5000")

    port = int(options.port)
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)

if __name__=='__main__':
    app.run(debug=True)
