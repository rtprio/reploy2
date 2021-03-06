from celery.task import task
from django.conf import settings
from deploy.util import parse_vget, parse_status, parse_log
from deploy.util import _remote_ssh, _remote_drush, _rsync_pull, _rsync_push, _check_site
from deploy.util import _remote_mysql, _local_cmd
from deploy.models import Site, Platform, Event, Statistic

import celery.result
import copy
import datetime
import glob
import locale
import logging
import os.path
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

def check_platform(platform):
    ok = True

    #check path
    (status, out, err) = _remote_ssh(platform, '[ -d %s ]' % (platform.path,))
    ok = ok and status 

    #check db/my.cnf
    (status, out, err) = _remote_ssh(platform, '[ -f %s ]' % ( '.my.cnf',))
    ok = ok and status
                                     
    #check for drush
    (status, out, err) = _remote_ssh(platform, 'which drush')
    ok = ok and status

    return ok

def reconcile_sites_dirs(platform):

    _ignored_files = [ "%s.php" %( platform.host,), 'http_host.pdx.edu.php', 'default']

    cmd = "ls %s/sites/" % (platform.path,)
    (status, out, err) = _remote_ssh(platform, cmd)

    paths = out.split('\n')
    sites= Site.objects.filter(platform=platform)

    for site in sites:
	if site.files_dir in paths:
            paths.remove(site.files_dir)

    for i in _ignored_files:
        if i in paths:
            paths.remove(i)

    return(True, 'Unaccounted for directories: %s' %(' \n'.join(paths),))

def reconcile_sites_databases(platform):

    _ignored_databases = ['information_schema', 'mysql', '_reploy2', '_reploy2_results']

    cmd = 'mysql -ss -e "%s"' % ('show databases;',)
    (status, out,err) = _remote_ssh(platform, cmd)

    unknown_databases = []

    if status == 0:
        dbs = filter(None, [ (i if i not in _ignored_databases else None) for i in out.split('\n')])
        for database in dbs:
            site = Site.objects.filter(database=database, platform=platform)
            if len(site) ==0:
                unknown_databases.append(database)
        return(True, 'Unaccounted for databases: %s' %(' \n'.join(unknown_databases),))
    else:
        return(False, 'Could not retrieve list of databases.')

@task
def reconcile_sites(platform):
    """ query/inventory unaccounted for sites/databases."""

    reconsile_sites_dirs(platform)
    reconcile_sites_databases(platform)

@task
def drush(site, cmd):
    (status, out, err) = _remote_drush(site, cmd)
    logger.debug("drush: %s" %(out,))
    logger.debug("drush: %s" %(err,))

    event = Event(  site=site, 
                    event='drush',
                    #user=request.user, 
                    task_id=drush.request.id, 
                    status= status==0,
                    message = "%s" %( out, ), # + "\n\n" + err,
                    )
    event.save()


    return (status==0, "%s\n%s" %(str(out), str(err)))

@task
def verify(site):

    _out = ''  

    (status, out, err) = _remote_drush(site, "vget maintenance_mode")
    _out += out

    site.unset_flag('error')
    site.unset_flag('unqueried')

    (status, out, err) = _remote_drush(site, "vget site_name")
    _out += out
    if status == 0:
        site.long_name = parse_vget('site_name', out)

        (status, extra, err) = _remote_drush(site, "vget site_name_extra")
        _out += extra
        if status == 0:
            site.long_name += ": " + parse_vget('site_name_extra', extra) 

        site.set_flag('ok')
        site.unset_flag('unqueried')
        site.unset_flag('not installed')
        site.save()
    else:
        site.unset_flag('ok')
        
    site.save()
    
    if status == 0:
        x = parse_vget('maintenance_mode', out)
        if x == 1:
            site.set_flag('maintenance')
        else:
            site.unset_flag('maintenance')
        site.save()
    else:
        site.unset_flag('maintenance')
        site.save()

    (status, out, err) = _remote_drush(site, "vget site_mail")
    _out += out
    if status == 0:
        site.contact_email = parse_vget('site_mail', out)
        site.save()
    else:
        site.set_flag('error')
        site.save()

    #TODO: possibly could need to flush varnish first before checking.
    status = _check_site(site)
    if status == 500 or status > 500:
        site.set_flag('error')
    if status == 404:
        site.set_flag('not installed')
    if not status == 200:
        site.unset_flag('ok')
        site.save()
    else:
        (status, out, err) = _remote_drush(site, "status")
        if status == 0:
            db = parse_status('Database name', out)
            logger.error('verify: updating database from %s to %s for site %s.' %(
                    site.database, db, str(site)))
            site.database = db
            site.save()

    # if we're this far, profile the site. asynchronously.
    get_site_status.delay(site)
    
    #create a log entry.
    event = Event(  site=site, 
                    event='verify',
                    #user=request.user, 
                    task_id=verify.request.id, 
                    status='',
                    message = _out,
                    )
    event.save()
    return (True, "This command completed sucessfully.")
    
@task       
def enable(site):
    (status, out, err) = _remote_drush(site, "vset --yes maintenance_mode 0")
    return (status == 0, parse_log(out))

@task
def disable(site):
    (status, out, err) = _remote_drush(site, "vset --yes maintenance_mode 1")
    return (status == 0, parse_log(out))

@task
def cacheclear(site):
    #TODO: cacheclear: needs to handle default
    status, _, _ = _remote_drush(site, "vpa" )
    status, out, _ = _remote_drush(site, "cc --yes all")
    return (status == 0, parse_log(out))

@task
def cron(site):
    (status, out, err) = _remote_drush(site, "cron")
    return (status == 0, parse_log(out))

    
def _backup_db(site, path):
    tmpdir = settings.TEMPORARY_PATH

    (status, remote_tempfile, err) = _remote_ssh(site.platform, 'mktemp %s' % (os.path.join(tmpdir, 'mysqldump.XXXXXXXX')))
    if status > 0:
        logger.error('_backup_db: could not open remote tempfile on host %s' % (site.platform.host,))
        return False

    logger.info('_backup_db: tempfile=%s site=%s' % (remote_tempfile, site))
    (status, out, err) = _remote_ssh(site.platform,
                                     'mysqldump -c --quick --single-transaction %s > %s' % ( site.database, remote_tempfile))
    if status > 0:
        logger.error('_backup_db: could not open mysqldump to tempfile on host %s' % (site.platform.host,))
        logger.error(out)
    else:
        local_tempfile = tempfile.mkdtemp()
        logger.info('_backup_db: local sql tempfile: %s' % (local_tempfile,))
        (s,o,e) = _rsync_pull(site.platform, remote_tempfile,
                              os.path.join(path,site.database + '.sql'))
        logger.error(o)
        logger.error(e)

        _remote_ssh(site.platform, 'rm %s' % (remote_tempfile,))
        if s == 0:
            return True
   
    return False


def _backup_files(site, path):
    (s,o,e) = _rsync_pull(site.platform, site.site_dir(), path)
    return s == 0

def _db_replace(old_site, new_site):

    cols = [
        ('field_revision_field_link', 'field_link_url'),
        ('field_revision_body', 'body_value'),
        ('field_revision_body', 'body_summary'),
        ('field_data_body', 'body_value'),
        ('field_data_body', 'body_summary'),
        ('field_revision_field_link', 'field_link_url'),
        ('field_data_field_link', 'field_link_url'),
        ('field_data_field_slide_link', 'field_slide_link_value'),
        ('field_revision_field_slide_link', 'field_slide_link_value'),
        ('menu_links', 'link_path'),
        ('block_custom', 'body'),
        # ('variable', 'value'),
        ]

    for i in cols:
        (table, col) = i
        sql1 = "UPDATE %s SET %s = REPLACE(%s, '%s', '%s');" %( table, col, col, old_site.site_uri, new_site.site_uri )
        sql2 = "UPDATE %s SET %s = REPLACE(%s, '%s', '%s');" % ( table, col, col, old_site.files_dir, new_site.files_dir )
                    
        (status, out, err) = _remote_ssh(new_site.platform, 'mysql %s -e "%s"' % (new_site.database,sql1) )
        (status, out, err) = _remote_ssh(new_site.platform, 'mysql %s -e "%s"' % (new_site.database,sql2) )
        
    return True

@task
def backup(site):
    """Returns a path to a backup or false if it doesn't succeed."""

    path = tempfile.mkdtemp(prefix='sdt',dir=settings.TEMPORARY_PATH)
    logger.info('backup: local temporary_path=%s' % (path,))

    status = 1
    cacheclear(site);

    db = _backup_db(site,path)
    fs = _backup_files(site,path)

    if not (db and fs):
        logger.info('backup: _backup_db was %s' % (str(db),) )
        logger.info('backup: _backup_fs was %s' % (str(fs),) )
        shutil.rmtree(path)
        if not backup.request.called_directly:
            event = Event(site=site, 
                          event='backup',
                          #user=request.user, 
                          task_id=backup.request.id, 
                          status=False,
                          message = '',
                          )
            event.save()
        return (False, "Backup didn't complete.")
                
    site_name = site.platform.host + '.' + site.short_name

    friendly_backup_path = os.path.join(settings.BACKUP_PATH,
                                        site_name + '-' + datetime.datetime.now().strftime('%Y%m%d.%H%M%S') + '.tgz')
    logger.info('backup: destination_path=%s' %(friendly_backup_path,))
    
    cmd = ['tar','-C', path, '-cpzf', friendly_backup_path, '.']
    logger.info('backup: command is: %s' % (cmd,))
    
    process = subprocess.Popen(cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output,stderr = process.communicate()
    status = process.poll()

    try:
        #remove temporary directory
        shutil.rmtree(path)
    except:
        logger.critical("backup: oops; can't delete: %s" % (path,))

    if not backup.request.called_directly:
      event = Event(site=site, 
                    event='backup',
                    #user=request.user, 
                    task_id=backup.request.id, 
                    status=status==0,
                    message = _out,
                    )
      event.save()
    return ( status==0, "backup is %s" %(friendly_backup_path,))

def _find_backup_file(site):
    """returns the most recent backup tarball."""
    logger.info('_find_backup_file: looking for a recent backup of site=%s' % (str(site),) )
    backup_location = settings.BACKUP_PATH
    site_name = site.platform.host + '.' + site.short_name
    l = glob.glob( os.path.join(backup_location, site_name + '-*') )
    l.sort()
    l.reverse()
    
    if len(l) > 0:
        logger.info('_find_backup_file: found a backup: ' + l[0])
        if len(l) > 1:
            logger.critical('_find_backup_file: site=%s has %d backups. ' % (str(site), len(l)) )
            for f in l[2:]:
                logger.critical('_find_backup_file: site=%s removing old backup=%s' % (str(site), f) )
                os.remove(f)

        return l[0]
    return None

@task
def varnishclear(site, pattern=None):
    """ flush the cache for a given site, or all sites for 'default'."""

    # this function is kinda special since it connects to multiple web frontends

    frontends = site.platform.canonical_host.split('|')
    purge = "purge req.http.host ~ %s && req.url ~ '^/%s/$'" % (site.platform.host,
                                                                site.short_name)
    #this is platform specific; could potentially use localhost depending on how varnish is set up
    #todo: move pattern/purge text into settings.py
    #todo: implement pattern parameter
    

    
    # copy of _remote_ssh here
    # TODO: refactor _remote_ssh
    for frontend in frontends:
        cmd = "/usr/bin/varnishadm -S /etc/varnish/secret -T %s:6082 %s" %(frontend, purge,)
        remote_cmd = ['ssh', frontend, cmd]
        logger.info("_varnish_flush (ssh): %s" % (' '.join(remote_cmd),) )
        process = subprocess.Popen(remote_cmd,
                                    stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        output,stderr = process.communicate()
        status = process.poll()


    return (True, "flush sent.")

@task
def migrate(site, new_platform):
    """Moves a site to a new platform. Site name, database remain the same."""
    if site.platform == new_platform:
        logger.critical("migrate: trying to migrate ontop of itself.")
        return (False, "trying to migrate ontop of itself.")

    backup_result, msg = backup(site)

    if not backup_result:
        logger.critical("migrate: backup didn't succeed, bail")
        return (False, "migrate: backup didn't succeed, bail. reason: " + msg)

    dest_site = None
    q = Site.objects.filter(short_name=site.short_name, platform=new_platform)
    if len(q) == 1:
        dest_site = q[0]
        #TODO: Do i want to keep this?
        wipe_site(dest_site)
    else:
        dest_site = copy.deepcopy(site)
        dest_site.id = None
        dest_site.platform = new_platform
        dest_site.save()
        dest_site.set_flag('unqueried')
        dest_site.save()
    
    if not is_clean(dest_site):
        logger.critical('migrate: destination site not clean, bail')
        return (False, "destination site not clean. Wipe destination site")

    # Find the backup to use
    tarball_path = _find_backup_file(site)
    if tarball_path == None:
        logger.critical('migrate: backup succeeded but now where is it? help.')
        return (False, "backup succeeded but now where is it? help.")

    #push the tarball_path into place.
    _rsync_push(new_platform, tarball_path, settings.TEMPORARY_PATH)

    # from the tarball, only extract foo.pdx.edu.baz into the sites/ directory
    # for some reason, these files have a leading ./ which i need to specify when extracting.

    #also, tarball contains the permenant home of this backup- we need to basename() it
    tarball = os.path.basename(tarball_path)

    
    (status, out, err) = _remote_ssh(new_platform,
                                     "tar -zxvf %s -C %s --exclude settings.php ./%s" % (os.path.join(settings.TEMPORARY_PATH,tarball),
                                                                  os.path.join(new_platform.path, 'sites'),
                                                                  'default' if site.short_name == 'default' else site.platform.host + '.' +  site.short_name,
                                                                  ) )

    (status, out, err) = _remote_ssh(new_platform,
                                     "tar -zxvf %s -C %s ./%s" % (os.path.join(settings.TEMPORARY_PATH,tarball),
                                                                  settings.TEMPORARY_PATH,
                                                                  site.database + '.sql'))
    
    #tarball components extracted: now we can remove it.
    _remote_ssh(new_platform,
                 "rm %s" % (os.path.join(settings.TEMPORARY_PATH,tarball),))
    
    #create and fill database
    _create_site_database(dest_site)


    #extract the sql dump out of the tarball locally.
    _local_cmd(['tar','-zxvf', tarball_path, '-C',  settings.TEMPORARY_PATH, './' + site.database + '.sql'])
    _local_cmd(['mysql','-h', dest_site.platform.database,
                '-e','\. %s' % ( os.path.join(settings.TEMPORARY_PATH, dest_site.database + '.sql'),),
                site.database
                ])

    #todo: stage database + replacements first.
    # this now hangs, so I'm attempting to connect to the databaes directly.
    # (status, out, err) = _remote_ssh(dest_site.platform,
    #                                  'mysql -n -v -v %s < %s' % (
    #                                      dest_site.database,
    #                                      remote_sql_path))

    #rename sitedir to the correct thing.
    _remote_ssh(new_platform, "mv %s %s" % (
                    os.path.join(new_platform.path,'sites',site.files_dir),
                    dest_site.site_dir() ))
                
                
    
    #put in a settings.php
    new_settings_php = tempfile.mkstemp()[1]
    
    dest_site.settings_php(new_settings_php)
    (status, out, err) = _rsync_push(dest_site.platform,
                                     new_settings_php,
                                     os.path.join(dest_site.site_dir(), 'settings.php'))
    #cleanup after our tempfile
    os.remove(new_settings_php)

    

    _create_site_dirs(dest_site)

    _set_site_permissions(dest_site)

    #search / replace database.
    status = _db_replace(site, dest_site)

    return (True, "Still more work to do.")

def _generate_database_name(s):
    """Generates a stripped version of the string, suitable for use as a mysql database. """

    s = s.strip().lower()
    
    return re.sub(r'-','_', s)
    

@task
def rename(site, new_site, clone=False):
    """ Renames a site. also renames the database. new_site may be a
    site or a string with the new short_name.  if clone is true, the
    existing site will not be deleted."""

    #TODO: genericify / wrap migrate so that it will handle clone, move & rename.

    # construct a dest_site
    dest_site = new_site
    
    if new_site.__class__ == str:
        dest_site = Site()
        dest_site.short_name = new_site
        dest_site.user = site.user 
        dest_site.database = _generate_database_name(new_site)
        dest_site.platform = site.platform
        dest_site.save()
    
    dest_site.set_flag('unqueried')
    dest_site.set_flag('not installed')
    dest_site.long_name = site.long_name + ' copy'
    dest_site.database = _generate_database_name(dest_site.short_name)
    dest_site.save()

    backup_result, msg = backup(site)

    if not backup_result:
        logger.critical("rename: backup didn't succeed, bail")
        return (False, "rename: backup didn't succeed, bail. reason: " + msg)

    tarball_path = _find_backup_file(site)
    if tarball_path == None:
        logger.critical('rename: backup succeeded but now where is it? help.')
        return (False, "backup succeeded but now where is it? help.")

    #push the tarball_path into place.
    _rsync_push(dest_site.platform, tarball_path, settings.TEMPORARY_PATH)

    # from the tarball, only extract foo.pdx.edu.baz into the sites/ directory
    # for some reason, these files have a leading ./ which i need to specify when extracting.

    #also, tarball contains the permenant home of this backup- we need to basename() it
    tarball = os.path.basename(tarball_path)

    
    (status, out, err) = _remote_ssh(dest_site.platform,
                                     "tar -zxvf %s -C %s ./%s" % (os.path.join(settings.TEMPORARY_PATH,tarball),
                                                                  settings.TEMPORARY_PATH,  # was os.path.join(dest_site.platform.path, 'sites'),
                                                                  'default' if site.short_name == 'default' else site.platform.host + '.' +  site.short_name,
                                                                  ) )

    (status, out, err) = _remote_ssh(dest_site.platform,
                                     "tar -zxvf %s -C %s ./%s" % (os.path.join(settings.TEMPORARY_PATH,tarball),
                                                                  settings.TEMPORARY_PATH,
                                                                  site.database + '.sql'))
    
    #tarball components extracted: now we can remove it.
    _remote_ssh(dest_site.platform,
                 "rm %s" % (os.path.join(settings.TEMPORARY_PATH,tarball),))
    

    #create and fill database
    #todo: stage database + replacements first.
    _create_site_database(dest_site)
    (status, out, err) = _remote_ssh(dest_site.platform,
                                     'mysql %s < %s' % (
                                         dest_site.database,
                                         os.path.join(settings.TEMPORARY_PATH, site.database + '.sql')
                                         ))

    #rename sitedir to the correct thing.
    _remote_ssh(dest_site.platform, "cp -Rp %s %s" % (
        os.path.join(settings.TEMPORARY_PATH, site.files_dir),
        dest_site.site_dir() ))
                
                
    
    #put in a settings.php
    new_settings_php = tempfile.mkstemp()[1]
    
    dest_site.settings_php(new_settings_php)
    (status, out, err) = _rsync_push(dest_site.platform,
                                     new_settings_php,
                                     os.path.join(dest_site.site_dir(), 'settings.php'))
    #cleanup after our tempfile
    os.remove(new_settings_php)

    

    _create_site_dirs(dest_site)

    _set_site_permissions(dest_site)

    #search / replace database.
    status = _db_replace(site, dest_site)

    return (True, "Still more work to do.")




    

def is_clean(site):
    """ return true of if there is no trace of the site. this includes symlink, database, sites directory"""

    clean = True
    message = ''

    (status, out, err) = _remote_ssh(site.platform, 'mysql mysql -e "use %s;"' % (site.database,))
    logger.info("is_clean: mysql: %d" % (status,) )
    clean = clean and status

    (status, out, err) = _remote_ssh(site.platform, '[ -d %s ]' % (site.site_dir(),))
    logger.info("is_clean: site_dir: %d" % (status,) )
    clean = clean and status

    if site.short_name == 'default':
        logger.info("is_clean: default site=%s is clean as it's going to be: %d" % (site, clean,) )
        return clean

    (status, out, err) = _remote_ssh(site.platform, '[ -L %s ]' % (site.site_symlink(),))
    logger.info("is_clean: site_symlink: %d" % (status,)) 
    clean = clean and status

    logger.info("is_clean: site=%s is clean: %d" % (site, clean,) )
    return clean


def _create_site_dirs(site):
    
    #link
    _remote_ssh(site.platform, '/bin/ln -s %s %s' % (
        site.platform.path,
        site.site_symlink(),
        ) )

    _remote_ssh(site.platform, 'mkdir %s' % (site.site_dir(), ))
    _remote_ssh(site.platform, 'chown sdtuser:apache %s' % (site.site_dir(), ))
    _remote_ssh(site.platform, 'chmod 2775 %s' % (site.site_dir(), ))
    
    for directory in site.site_files_dir():
        _remote_ssh(site.platform, 'mkdir %s' % (directory, ))
        _remote_ssh(site.platform, 'chown sdtuser:apache %s' % (directory, ))
        _remote_ssh(site.platform, 'chmod 2775 %s' % (directory, ))
        
    return True

def _create_site_database(site):
    (status, out, err) = _remote_ssh(site.platform, 'mysql -e "create database %s;"' % (site.database,))
    return status == 0


def _create_settings_php(site):
    settings = tempfile.mkstemp()[1]
    site.settings_php(settings)

    #post install this fails since drush chown'd settings.php.
    _remote_ssh(site.platform, 'chmod 755 %s' %( site.site_dir(), ) )
                
    (status, out, err) = _rsync_push(site.platform,
                                     settings,
                                     os.path.join( site.site_dir(), 'settings.php')
                                     )
    return status == 0

def _set_site_permissions(site):
    # dev servers have different permissions
    (status, out, err) = _remote_ssh(site.platform, 'chmod %d %s;' % ( 666 if site.platform.use == 'dev' else 664,
                                                                       os.path.join( site.site_dir(), 'settings.php' ),) )
        
    (status, out, err) = _remote_ssh(site.platform, 'chmod 775 %s;' % ( site.site_dir(), ) )

    #TODO: perhaps do chowning that's currently in _create_site_dirs here?
    
    return status == 0

@task
def create(site, force=False):
    #create db
    logger.info("create: ")

    if not is_clean(site) and not force:
        logger.info("create: forced")
        return (False, "Destination site is not clean.")

    if _create_site_database(site) or force:
         
        logger.info("create: sitedir: %s" % (site.site_dir(),))
        if _create_site_dirs(site) or force:

            # 'not installed' sites don't use the shared tables. this is necessary when installing.
            # after installed, use the shared tables so cas users may log in.
            site.set_flag('not installed')
            site.save()
            settings = _create_settings_php(site)

            if settings:
                install_status, output, err = _remote_drush(site, "site-install -y --site-name='%s' --sites-subdir='%s' --site-mail='%s' %s"
                                                        %( site.long_name,
                                                           'default' if site.short_name == 'default' else site.platform.host + '.' +  site.short_name,
                                                           site.contact_email,
                                                           site.profile) )
                if install_status == 0:
                    site.unset_flag('not installed')
                    site.set_flag('unqueried')
                    site.save()
                    # this updated settings.php uses the shared tables.
                    settings = _create_settings_php(site)
                    perm = _set_site_permissions(site)
                    return (True, output)
                site.set_flag('error')
                site.save()

                return (False, str(output) + str(err) )

            else:
                logger.error("create(): create site failed. %s" %(output,))
        else:
            logger.error("create(): create sitedirs failed")
            
            wipe_site(site)

    else:
        logger.error('create database failed %s' % (site.database,))
        return (False, "creating database failed.")

    
    #drush --uri=http://localhost/foo site-install --sites-subdir=localhost.foo  --site-name=foo psu_primary 

    logger.info("create: leave")
    return (True, "unknown")
    
@task    
def wipe_site(site):
    logger.debug("wipe_site(): site=%s" %(site,) )

    if is_clean(site):
        logger.error("wipe_site(): site=%s is already clean." %(site,))
        return (True, "this site is already clean.")

    # chmod some files that the installer restricts.
    _remote_ssh(site.platform, 'chmod -R 777 %s' %(site.site_dir(),))
    (status, err, out ) = _remote_ssh(site.platform, 'rm -Rf %s' % (site.site_dir(),))

    _remote_ssh(site.platform, 'mysql -e "drop database %s;"' % (site.database,))

    # default sites don't have symlinks.
    if not site.short_name == 'default':
        _remote_ssh(site.platform, 'unlink %s' % (site.site_symlink(),))

    #TODO: verify the directory is gone.
    #TODO: find site.site_dir() -type f  for any leftovers. log and return them.

    return (True, str(err) + str(out))


def get_celery_worker_status():
    ERROR_KEY = "ERROR"
    try:
        from celery.task.control import inspect
        insp = inspect()
        d = insp.stats()
        if not d:
            d = { ERROR_KEY: 'No running Celery workers were found.' }
    except IOError as e:
        from errno import errorcode
        msg = "Error connecting to the backend: " + str(e)
        if len(e.args) > 0 and errorcode.get(e.args[0]) == 'ECONNREFUSED':
            msg += ' Check that the RabbitMQ server is running.'
        d = { ERROR_KEY: msg }
    except ImportError as e:
        d = { ERROR_KEY: str(e)}
    return d

@task
def get_site_status(site):
    get_site_du(site)
    get_node_count(site)
    get_cron_last(site)
    get_file_counts(site)

    return True



def get_site_du(site):

    (status, out,err) = _remote_ssh(site.platform,
                                    "du -ks %s" % (site.site_dir(),) )
    if status == 0:
        tmp = out.split('\t')
        kilobytes = int(tmp[0])

        locale.setlocale(locale.LC_ALL, '')
        s = locale.format("%d", kilobytes/1024, grouping=True) + ' megabytes'
        
        x = Statistic(site=site, metric='disk_usage', value=s)
        x.save()
                      
        return {'disk_usage': s}
    return {}

def get_node_count(site):
    out = _remote_mysql(site, "select count(distinct nid) as nodes from node;")
    if not out == None:
        x = Statistic(site=site, metric='node_count', value=int(out))
        x.save()
        
        return {'node_count': int(out) }
    return {}
    
def get_cron_last(site):
    (status, out, err) = _remote_drush(site, "vget cron_last") 
    cron_last = parse_vget('cron_last', out)
    if cron_last:
        t = datetime.datetime.fromtimestamp(float(cron_last))
        time_format = t.strftime(settings.TIME_FORMAT)
        x = Statistic(site=site, metric='cron_last', value=time_format)
        x.save()

        return {'cron_last': time_format }
    else:
        return {}
    
def get_file_counts(site):
    out = _remote_mysql(site, "select count(*) as f from file_managed;")
    if not out == None:
        x = Statistic(site=site, metric='file_managed', value=int(out))
        x.save()

    out = _remote_mysql(site, "select count(*) as f from file_usage;")
    if not out == None:
        x = Statistic(site=site, metric='file_used', value=int(out))
        x.save()
        
        
    


