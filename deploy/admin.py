from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.http import HttpResponseRedirect, HttpResponse

from deploy.models import Platform, Site, Status, Event, Statistic
from deploy.actions import verify, create, enable, disable, wipe_site, cacheclear, backup


class SiteAdmin(admin.ModelAdmin):
    list_display = ['link', 'short_name','long_name', 'contact_email',
                    'platform','show_status','manage' ]
    list_filter = ['platform', 'user', 'status']
    list_display_links = ['short_name']
    search_fields = ['long_name', 'short_name']
    ordering = ['long_name', 'short_name']
    actions = ['site_online', 'site_offline', 'site_verify', 'site_create',
               'site_cacheclear', 'site_migrate', 'site_wipe', 'site_backup', 'site_drush']
#    exclude =['status']

    def site_online(self, request, queryset):
        for site in queryset:
            ctask = enable.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event="online")
            event.save()
            messages.add_message(request, messages.INFO, "%s has been submitted to be removed from maintenance: %s" % ( site, ctask.task_id) )
                
    def site_offline(self, request, queryset):
        for site in queryset:
            ctask = disable.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event="offline")
            event.save()
            messages.add_message(request, messages.INFO, "%s has been submitted to be entered into maintenance: %s" % ( site, ctask.task_id) )
                
    def site_verify(self, request,queryset):
         for site in queryset:
             ctask = verify.delay(site)
             event = Event( task_id=ctask.task_id, site=site, user=request.user, event='verify')
             event.save()
             messages.add_message(request, messages.INFO, "%s has been submitted for verification: %s" % ( site, ctask.task_id) )

    def site_create(self, request, queryset):
        for site in queryset:
            ctask = create.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event='create')
            event.save()
            messages.add_message(request, messages.INFO, "%s has been submitted to be created: %s" % ( site, ctask.task_id) )

    def site_wipe(self, request, queryset):
        for site in queryset:
            ctask = wipe_site.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event='wipe')
            event.save()
            messages.add_message(request, messages.INFO, "%s has been submitted to be removed: %s" % ( site, ctask.task_id) )

    def site_backup(self,request, queryset):
        for site in queryset:
            ctask = backup.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event='backup')           
            event.save()
            messages.add_message(request, messages.INFO, "%s has been submitted to be backuped: %s" % ( site, ctask.task_id) )

    def site_restore(self,request, queryset):
        for site in queryset:
            ctask = restore.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event='restore')
            event.save()
            messages.add_message(request, messages.INFO, "%s has been submitted to be restored: %s" % ( site, ctask.task_id) )

    def site_cacheclear(self, request, queryset):
        for site in queryset:
            ctask = cacheclear.delay(site)
            event = Event( task_id=ctask.task_id, site=site, user=request.user, event='cacheclear')
            event.save()
            messages.add_message(request, messages.INFO, "The cache of site %s has been cleared: %s" % ( site, ctask.task_id) )
 
    def site_migrate(self, request, queryset):
        selected = request.POST.getlist(admin.ACTION_CHECKBOX_NAME)
        ct = ContentType.objects.get_for_model(queryset.model)
        return HttpResponseRedirect("/site-migrate?ct=%s&ids=%s" % (ct.pk, ",".join(selected)))

    def site_drush(self, request, queryset):
        selected = request.POST.getlist(admin.ACTION_CHECKBOX_NAME)
        ct = ContentType.objects.get_for_model(queryset.model)
        return HttpResponseRedirect("/site-drush?ct=%s&ids=%s" % (ct.pk, ",".join(selected)))
                     
        
    site_backup.short_description     = 'Backup.'
    site_cacheclear.short_description = 'Cache clear.'
    site_create.short_description     = 'Install.'
    site_drush.short_description      = 'Drush.'
    site_offline.short_description    = 'Enable.'
    site_online.short_description     = 'Disable.'
    site_restore.short_descriptiono   = 'Restore.'
    site_verify.short_description     = 'Verify.'
    site_wipe.short_description       = 'Wipe out.'

class EventAdmin(admin.ModelAdmin):
    list_display = ['site','event','user','date','status','message','task_id']
    list_filter = ['user','status', 'event']
    list_display_links = ['task_id']
    
admin.site.register(Site,SiteAdmin)
admin.site.register(Platform)
admin.site.register(Status)
admin.site.register(Event,EventAdmin)
admin.site.register(Statistic)


