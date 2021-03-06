import datetime
import logging
import shlex
import subprocess
import urllib2

logger = logging.getLogger(__name__)

def parse_vget(variable, output):
    """ Extract a desired variable from a 'drush vget'. """
    for line in output.split('\n'):
        if line.find(variable + ':') > -1:
            quoted = line.replace(variable + ':','').strip()
            quoted = quoted.strip("'")
            quoted = quoted.strip('"')
            return quoted
    return False

def parse_status(variable, output):
    """ Extract a desired variable from a 'drush status'. """
    for line in output.split('\n'):
        if line.find(variable) > -1:
            quoted = line.split(':')[-1].strip()
            return quoted.strip('"')
    return False    

def parse_log(output):
    """ un-cruftify the drush console output. """

    #this is junk that drush spits out:
    for token in ['[success]','[warning]','[error]','[ok]','[status]']:
        output = output.replace(token,'').strip()
    output = output.replace('  ',' ')
    return output.strip()

def _local_cmd(cmd):
    """ just a lazy wrapper for popen, but follows the same pattern as _remote_ssh."""    
    logger.info("_local_cmd: %s" % (' '.join(cmd),) )
    begin = datetime.datetime.now()
    process = subprocess.Popen(cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output,stderr = process.communicate()
    status = process.poll()

    if not status == 0:
        logger.info("_local_cmd: command output: %s" % (output,))
        logger.info("_local_cmd: command error: %s" % (stderr,))

    t = datetime.datetime.now() - begin
    logger.info("_local_cmd: took %d seconds. exit status %d." % ( t.seconds,status))
        
    return (status,output,stderr)


def _remote_ssh(platform, cmd):
    """ returns tuple of (exit status, stdout, sdterr) """

    #logger.info("_remote_ssh: enter")
    begin = datetime.datetime.now()
    remote_cmd = ['ssh', platform.ssh_host, cmd]
    logger.info("_remote_ssh: %s" % (' '.join(remote_cmd),))

    process = subprocess.Popen(remote_cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output, stderr = process.communicate()
    status = process.poll()

    if not status == 0:
        if status == 127:
            (s,out, err) = _remote_ssh(platform, "echo $PATH")
            logger.critical("_remote_ssh: not found: is `%s' in the remote path (%s)" % ( cmd.split(' ')[0],out))
            
        
        logger.info("_remote_ssh: command output: %s" % (output,))
        logger.info("_remote_ssh: command error: %s" % (stderr,))

    t = datetime.datetime.now() - begin
    logger.info("_remote_ssh: took %d seconds. exit status %d." % ( t.seconds,status))
        
    return (status,output,stderr)

def _remote_drush(site, args):
    """ run drush command <drush args> on remote site"""
    uri = site.short_name
    if uri == 'default':
        uri = ''
        
    cmd = "drush --root='%s' --uri='http://%s/%s' %s" % (site.platform.path.strip(),
                                                             site.platform.host.strip(),
                                                             uri.strip(), args)
    return _remote_ssh(site.platform, cmd)

def _remote_mysql(site, query):
    """ run a mysql query on the site's database. """

    cmd = 'mysql %s -ss -e "%s"' % (site.database, query)
    logger.info('_remote_mysql(): site=%s query="%s"' %( site, query))
    
    (status, out,err) = _remote_ssh(site.platform, cmd)
    
    if status == 0:
        logger.info('_remote_mysql(): result=%s' %(out,))
        return out

    logger.info('_remote_mysql(): out="%s" err="%s"' %(out,err))

    return None
    

def _rsync_pull(platform, remote, local):
    path = "%s:%s" % (platform.ssh_host, remote)
    cmd = ['rsync','--archive', '--numeric-ids', '-pvv', path, local]
    logger.info("_rsync_pull: from %s remote=%s local=%s" % ( platform, remote, local))
    logger.info("_rsync_pull: %s" % ( ' '.join(cmd),))
    begin = datetime.datetime.now()
    process = subprocess.Popen(cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output,stderr = process.communicate()
    status = process.poll()
    t = datetime.datetime.now() - begin
    logger.info('_rsync_pull: took %d seconds. returned %d.' % (t.seconds, status))
    return (status,output,stderr)

def _rsync_push(platform, local, remote):
    logger.info("_rsync_push: to %s remote=%s local=%s" % ( platform, remote, local))
    path = "%s:%s" % (platform.ssh_host, remote)
    cmd = ['rsync','--archive', '--numeric-ids', '-pvv', local, path]
    logger.info("_rsync_push: %s" % ( ' '.join(cmd),))
    begin = datetime.datetime.now()
    process = subprocess.Popen(cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output,stderr = process.communicate()
    status = process.poll()
    t = datetime.datetime.now() - begin
    logger.info('_rsync_push: took %d seconds. returned %d.' % (t.seconds, status))
    return (status,output,stderr)


def _check_site(site):
    logger.info('_check_site: site: ' + str(site))

    http_status = 200
    
    req = urllib2.Request(str(site))
    try:
        urllib2.urlopen(req)
    except urllib2.URLError, e:
        http_status = 550
    except urllib2.HTTPError, e:
        http_status = e.code
    logger.critical('_check_site: site=%s httpstatus=%d' % (str(site),http_status))
        
    return http_status
