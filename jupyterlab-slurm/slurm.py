import json
import re
import shlex
import asyncio
import html
import os

from notebook.base.handlers import IPythonHandler
from tornado.web import MissingArgumentError

class ShellExecutionHandler(IPythonHandler):
    async def run_command(self, command, stdin=None):
        commands = shlex.split(command)
        process = await asyncio.create_subprocess_exec(*commands,
                                                           stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE,
                                                           stdin=stdin)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0) 
        # decode stdout and from bytes to str, and return stdout, stderr, and returncode  
        return (stdout.decode().strip(), stderr.decode().strip(), process.returncode)


### Conventions:
## Query arguments: always settings for how to use or options provided by a SLURM command.
## Body arguments: always job designators, e.g. job ID, paths to SLURM scripts, input streams of SLURM script contents, etc.
## Path arguments: always commands (including commands sent to `scontrol`, e.g. `scontrol hold`/`scontrol resume`)

## Unsurprisingly, the job ID's are always (for scancel and scontrol) the body argument named 'jobID'

# Since this is idempotent, hypothetically one could also use PUT instead of DELETE here.
class ScancelHandler(ShellExecutionHandler):
    # Add `-H "Authorization: token <token>"` to the curl command for any DELETE request
    async def delete(self):
        jobIDs = self.get_body_arguments('jobID')
        stdout, stderr, returncode = await self.run_command(' '.join(['scancel'] + jobIDs))
        print("SDOUT: ", stdout)
        print("STDERR: ", stderr)
        print("returncode: ", returncode)
        if stdout:
            responseMessage = stdout
        else:
            responseMessage = stderr
        self.finish({"responseMessage": responseMessage, "returncode": returncode})


# scontrol isn't idempotent, so PUT isn't appropriate, and in general scontrol only modifies a subset of properties, so POST also is not ideal
class ScontrolHandler(ShellExecutionHandler):
    # Add `-H "Authorization: token <token>"` to the curl command for any PATCH request
    async def patch(self, command):
        job_list = ','.join(self.get_body_arguments('jobID'))
        stdout, stderr, returncode = await self.run_command(' '.join(['scontrol', command, job_list]))
        print("SDOUT: ", stdout)
        print("STDERR: ", stderr)
        if stdout:
            responseMessage = stdout
        else:
            responseMessage = stderr
        self.finish({"responseMessage": responseMessage, "returncode": returncode})

# sbatch clearly isn't idempotent, and resource ID (i.e. job ID) isn't known when running it, so only POST works for the C in CRUD here, not PUT
class SbatchHandler(ShellExecutionHandler):
    # Add `-H "Authorization: token <token>"` to the curl command for any POST request
    async def post(self):
        def string_to_file(string):
            file_object = open('temporary_file.temporary', 'w')
            file_object.write(string)
            file_object.close()
            return
        scriptIs = self.get_query_argument('scriptIs')
        # Have two options to specify SLURM script in the request body: either with a path to the script, or with the script's text contents
        if scriptIs:
            if scriptIs == 'path':
                script_path = self.get_body_argument('script')
                stdout, stderr = await self.run_command('sbatch '+script_path)
            elif scriptIs == 'contents':
                script_contents = self.get_body_argument('script')
                string_to_file(script_contents)
                stdout, stderr, returncode = await self.run_command('sbatch', stdin=open('temporary_file.temporary','rb'))
                os.remove('temporary_file.temporary')
            else:
                raise Exception('The query argument scriptIs needs to be either \'path\' or \'contents\'.')
        else:
            raise MissingArgumentError('scriptIs')
        print("SDOUT: ", stdout)
        print("STDERR: ", stderr)
        if stdout:
            responseMessage = stdout
        else:
            responseMessage = stderr
    # jobID = re.compile('([0-9]+)$').search(stdout).group(1)
        self.finish({"responseMessage": responseMessage, "returncode": returncode})

# all squeue does is request information from SLURM scheduler, which is idempotent (for the "server-side"), so clearly GET request is appropriate here
class SqueueHandler(ShellExecutionHandler):
    async def get(self):
        # what to keep in the mind if we want to add a view user's jobs only button -- it would just add the -u flag to the command
        # userOnly = self.get_query_argument('userOnly')
        # if (userOnly == 'true'):
        #    data, stderr = await self.run_command('squeue -u $USER -o "%.18i %.9P %.8j %.8u %.2t %.10M %.6D %R" -h')
        # elif (userOnly == 'false'):
        #    data, stderr = await self.run_command('squeue -o "%.18i %.9P %.8j %.8u %.2t %.10M %.6D %R" -h')
        
        # squeue -h automatically removes the header row
        # -o <format string> ensures that the output is in a format expected by the extension
        # Hard-coding this is not great -- ideally we would allow the user to customize this, or have the default output be the user's output
        data, stderr = await self.run_command('squeue -o "%.18i %.9P %.8j %.8u %.2t %.10M %.6D %R" -h')
        lines = data.splitlines()
        data_dict = {}
        data_list = []
        for line in lines:
            # maxsplit=7 so we can still display squeue entries with final columns with spaces like the following:
            # (burst_buffer/cray: dws_data_in: DataWarp REST API error: offline namespaces: [34831] - ask a system administrator to consult the dwmd log for more information
            if len(line.split(maxsplit=7)) == 8:
                # html.escape because some job ID's might have '<'s and similar characters in them.
                # Also, hypothetically we could be Bobbytable'd without html.escape here,
                # e.g. if someone had as a jobname '<script>virus.js</script>'.
                data_list += [[(html.escape(entry)).strip() for entry in line.split(maxsplit=7)]]
            else:
                continue
        data_dict['data'] = data_list[:]
        # finish(chunk) writes chunk to the output 
        # buffer and ends the HTTP request
        self.finish(json.dumps(data_dict))
