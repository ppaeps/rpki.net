# $Id$
"""
Copyright (C) 2010, 2011  SPARTA, Inc. dba Cobham Analytic Solutions

Permission to use, copy, modify, and distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND SPARTA DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS.  IN NO EVENT SHALL SPARTA BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
"""

import email.message, email.utils, mailbox
import os, os.path
import sys

from django.contrib.auth.decorators import login_required
from django.contrib import auth
from django.shortcuts import get_object_or_404, render_to_response
from django.utils.http import urlquote
from django.template import RequestContext
from django import http
from django.views.generic.list_detail import object_list
from django.conf import settings
from django.core.urlresolvers import reverse

from rpki.gui.app import models, forms, glue, misc, AllocationTree
from rpki.gui.app.asnset import asnset

debug = False

def my_login_required(f):
    """
    A version of django.contrib.auth.decorators.login_required
    that will fail instead of redirecting to the login page when
    the user is not logged in.

    For use with the rpkidemo service URLs where we want to detect
    failure to log in.  Otherwise django will return code 200 with
    the login form, and fools rpkidemo.
    """
    def wrapped(request, *args, **kwargs):
        if  not request.user.is_authenticated():
            return http.HttpResponseForbidden()
        return f(request, *args, **kwargs)

    return wrapped

# For each type of object, we have a detail view, a create view and
# an update view.  We heavily leverage the generic views, only
# adding our own idea of authorization.

def handle_required(f):
    @login_required
    def wrapped_fn(request, *args, **kwargs):
        if 'handle' not in request.session:
            if request.user.is_superuser:
                conf = models.Conf.objects.all()
            else:
                conf = models.Conf.objects.filter(owner=request.user)
            if conf.count() == 1:
                handle = conf[0]
            elif conf.count() == 0:
                return render('myrpki/conf_empty.html', {}, request)
                #return http.HttpResponseRedirect('/myrpki/conf/add')
            else:
                # Should reverse the view for this instead of hardcoding
                # the URL.
                return http.HttpResponseRedirect(
                        reverse(conf_list) + '?next=' + urlquote(request.get_full_path()))
            request.session[ 'handle' ] = handle
        return f(request, *args, **kwargs)
    return wrapped_fn

def render(template, context, request):
    return render_to_response(template, context,
            context_instance=RequestContext(request))

@handle_required
def dashboard(request):
    '''The user's dashboard.'''
    handle = request.session[ 'handle' ]
    # ... pick out data for the dashboard and return it
    # my parents
    # the resources that my parents have given me
    # the resources that I have accepted from my parents
    # my children
    # the resources that I have given my children
    # my roas

    # get list of ASNs used in my ROAs
    roa_asns = [r.asn for r in handle.roas.all()]
    asns=[]
    for a in models.Asn.objects.filter(from_cert__parent__in=handle.parents.all()):
        f = AllocationTree.AllocationTreeAS(a)
        if f.unallocated():
            asns.append(f)

    prefixes = []
    for p in models.AddressRange.objects.filter(from_cert__parent__in=handle.parents.all()):
        f = AllocationTree.AllocationTreeIP.from_prefix(p)
        if f.unallocated():
            prefixes.append(f)

    asns.sort(key=lambda x: x.range.min)
    prefixes.sort(key=lambda x: x.range.min)

    return render('myrpki/dashboard.html', { 'conf': handle, 'asns': asns,
        'ars': prefixes }, request)

@login_required
def conf_list(request):
    """Allow the user to select a handle."""
    if request.user.is_superuser:
        queryset = models.Conf.objects.all()
    else:
        queryset = models.Conf.objects.filter(owner=request.user)
    return object_list(request, queryset,
            template_name='myrpki/conf_list.html', template_object_name='conf', extra_context={ 'select_url' : reverse(conf_select) })

@login_required
def conf_select(request):
    '''Change the handle for the current session.'''
    if not 'handle' in request.GET:
        return http.HttpResponseRedirect('/myrpki/conf/select')
    handle = request.GET['handle']
    next_url = request.GET.get('next', reverse(dashboard))
    if next_url == '':
        next_url = reverse(dashboard)

    if request.user.is_superuser:
        conf = models.Conf.objects.filter(handle=handle)
    else:
        # since the handle is passed in as a parameter, need to verify that
        # the user is actually in the group
        conf = models.Conf.objects.filter(handle=handle,
                owner=request.user)
    if conf:
        request.session['handle'] = conf[0]
        return http.HttpResponseRedirect(next_url)

    return http.HttpResponseRedirect(reverse(conf_list) + '?next=' + next_url)

def serve_xml(content, basename):
    resp = http.HttpResponse(content , mimetype='application/xml')
    resp['Content-Disposition'] = 'attachment; filename=%s.xml' % (basename, )
    return resp

@handle_required
def conf_export(request):
    """Return the identity.xml for the current handle."""
    handle = request.session['handle']
    return serve_xml(glue.read_identity(handle.handle), 'identity')

@handle_required
def parent_view(request, parent_handle):
    """Detail view for a particular parent."""
    handle = request.session['handle']
    parent = get_object_or_404(handle.parents, handle__exact=parent_handle)
    return render('myrpki/parent_view.html', { 'parent': parent }, request)

def get_parents_or_404(handle, obj):
    '''Return the Parent object(s) that the given address range derives
    from, or raise a 404 error.'''
    cert_set = misc.top_parent(obj).from_cert.filter(parent__in=handle.parents.all())
    if cert_set.count() == 0:
        raise http.Http404, 'Object is not delegated from any parent'
    return [c.parent for c in cert_set]

@handle_required
def asn_view(request, pk):
    '''view/subdivide an asn range.'''
    handle = request.session['handle']
    obj = get_object_or_404(models.Asn.objects, pk=pk)
    # ensure this resource range belongs to a parent of the current conf
    parent_set = get_parents_or_404(handle, obj)
    roas = handle.roas.filter(asn=obj.lo) # roas which contain this asn
    unallocated = AllocationTree.AllocationTreeAS(obj).unallocated()
    
    return render('myrpki/asn_view.html',
            { 'asn': obj, 'parent': parent_set, 'roas': roas,
                'unallocated' : unallocated }, request)

@handle_required
def child_view(request, child_handle):
    '''Detail view of child for the currently selected handle.'''
    handle = request.session['handle']
    child = get_object_or_404(handle.children, handle__exact=child_handle)

    return render('myrpki/child_view.html', { 'child': child }, request)

class PrefixView(object):
    '''Extensible view for address ranges/prefixes.  This view can be
    subclassed to add form handling for editing the prefix.'''

    def __init__(self, request, pk, form_class=None):
        self.handle = request.session['handle']
        self.obj = get_object_or_404(models.AddressRange.objects, pk=pk)
        # ensure this resource range belongs to a parent of the current conf
        self.parent_set = get_parents_or_404(self.handle, self.obj)
        self.form = None
        self.form_class = form_class
        self.request = request
 
    def __call__(self, *args, **kwargs):
        if self.request.method == 'POST':
            resp = self.handle_post()
        else:
            resp = self.handle_get()

        # allow get/post handlers to return a custom response
        if resp:
            return resp
        
        u = AllocationTree.AllocationTreeIP.from_prefix(self.obj).unallocated()

        return render('myrpki/prefix_view.html',
                { 'addr': self.obj, 'parent': self.parent_set, 'unallocated': u, 'form': self.form },
                self.request)

    def handle_get(self):
        '''Virtual method for extending GET handling.  Default action is
        to call the form class constructor with the prefix object.'''
        if self.form_class:
            self.form = self.form_class(self.obj)

    def form_valid(self):
        '''Virtual method for handling a valid form.  Called by the default
        implementation of handle_post().'''
        pass
 
    def handle_post(self):
        '''Virtual method for extending POST handling.  Default implementation
        creates a form object using the form_class in the constructor and passing
        the prefix object.  If the form's is_valid() method is True, it then
        invokes this class's form_valid() method.'''
        resp = None
        if self.form_class:
            self.form = self.form_class(self.obj, self.request.POST)
            if self.form.is_valid():
                resp = self.form_valid()
        return resp

@handle_required
def address_view(request, pk):
    return PrefixView(request, pk)()

class PrefixSplitView(PrefixView):
    '''Class for handling the prefix split form.'''
    def form_valid(self):
        r = misc.parse_resource_range(self.form.cleaned_data['prefix'])
        obj = models.AddressRange(lo=str(r.min), hi=str(r.max), parent=self.obj)
        obj.save()
        return http.HttpResponseRedirect(obj.get_absolute_url())

@handle_required
def prefix_split_view(request, pk):
    return PrefixSplitView(request, pk, form_class=forms.PrefixSplitForm)()

class PrefixAllocateView(PrefixView):
    '''Class to handle the allocation to child form.'''
    def handle_get(self):
        self.form = forms.PrefixAllocateForm(
                self.obj.allocated.pk if self.obj.allocated else None,
                self.handle.children.all())

    def handle_post(self):
        self.form = forms.PrefixAllocateForm(None, self.handle.children.all(), self.request.POST)
        if self.form.is_valid():
            self.obj.allocated = self.form.cleaned_data['child']
            self.obj.save()
            glue.configure_resources(self.handle)
            return http.HttpResponseRedirect(self.obj.get_absolute_url())

@handle_required
def prefix_allocate_view(request, pk):
    return PrefixAllocateView(request, pk)()

def add_roa_requests(handle, prefix, asns, max_length):
    for asid in asns:
        if debug:
            print 'searching for a roa for AS %d containing %s-%d' % (asid, prefix, max_length)
        req_set = prefix.roa_requests.filter(roa__asn=asid, max_length=max_length)
        if not req_set:
            if debug:
                print 'no roa for AS %d containing %s-%d' % (asid, prefix, max_length)

            # find ROAs for prefixes derived from the same resource cert
            # as this prefix
            certs = misc.top_parent(prefix).from_cert.all()
            roa_set = handle.roas.filter(asn=asid, cert__in=certs)

            # FIXME: currently only creates a ROA/request for the first
            # resource cert, not all of them
            if roa_set:
                roa = roa_set[0]
            else:
                if debug:
                    print 'creating new roa for AS %d containg %s-%d' % (asid, prefix, max_length)
                # no roa is present for this ASN, create a new one
                roa = models.Roa.objects.create(asn=asid, conf=handle,
                        active=False, cert=certs[0])
                roa.save()

            req = models.RoaRequest.objects.create(prefix=prefix, roa=roa,
                    max_length=max_length)
            req.save()

class PrefixRoaView(PrefixView):
    '''Class for handling the ROA creation form.'''
    def form_valid(self):
        asns = asnset(self.form.cleaned_data['asns'])
        add_roa_requests(self.handle, self.obj, asns, self.form.cleaned_data['max_length'])
        glue.configure_resources(self.handle)
        return http.HttpResponseRedirect(self.obj.get_absolute_url())
 
@handle_required
def prefix_roa_view(request, pk):
    return PrefixRoaView(request, pk, form_class=forms.PrefixRoaForm)()

class PrefixDeleteView(PrefixView):
    def form_valid(self):
        if self.form.cleaned_data['delete']:
            self.obj.delete()
            return http.HttpResponseRedirect('/myrpki/')
 
@handle_required
def prefix_delete_view(request, pk):
    return PrefixDeleteView(request, pk, form_class=forms.PrefixDeleteForm)()

@handle_required
def roa_request_delete_view(request, pk):
    '''Remove a roa request from a particular prefix.'''
    handle = request.session['handle']
    obj = get_object_or_404(models.RoaRequest.objects, pk=pk)
    prefix = obj.prefix
    # ensure this resource range belongs to a parent of the current conf
    parent_set = get_parents_or_404(handle, prefix)

    roa = obj.roa
    obj.delete()
    if not roa.from_roa_request.all():
        roa.delete()
    glue.configure_resources(handle)

    return http.HttpResponseRedirect(prefix.get_absolute_url())

@handle_required
def asn_allocate_view(request, pk):
    handle = request.session['handle']
    obj = get_object_or_404(models.Asn.objects, pk=pk)
    # ensure this resource range belongs to a parent of the current conf
    parent_set = get_parents_or_404(handle, obj)

    if request.method == 'POST':
        form = forms.PrefixAllocateForm(None, handle.children.all(), request.POST)
        if form.is_valid():
            obj.allocated = form.cleaned_data['child']
            obj.save()
            glue.configure_resources(handle)
            return http.HttpResponseRedirect(obj.get_absolute_url())
    else:
        form = forms.PrefixAllocateForm(obj.allocated.pk if obj.allocated else None,
                handle.children.all())

    return render('myrpki/asn_view.html', { 'form': form,
        'asn': obj, 'form': form, 'parent': parent_set }, request)

# this is similar to handle_required, except that the handle is given in URL
def handle_or_404(request, handle):
    "ensure the requested handle is available to this user"
    if request.user.is_superuser:
        conf_set = models.Conf.objects.filter(handle=handle)
    else:
        conf_set = models.Conf.objects.filter(owner=request.user, handle=handle)
    if not conf_set:
        raise http.Http404, 'resource handle not found'
    return conf_set[0]

def serve_file(handle, fname, content_type, error_code=404):
    content, mtime = glue.read_file_from_handle(handle, fname)
    resp = http.HttpResponse(content , mimetype=content_type)
    resp['Content-Disposition'] = 'attachment; filename=%s' % (os.path.basename(fname), )
    resp['Last-Modified'] = email.utils.formatdate(mtime, usegmt=True)
    return resp

@my_login_required
def download_csv(request, self_handle, fname):
    conf = handle_or_404(request, self_handle)
    return serve_file(conf.handle, fname + '.csv', 'text/csv')

def download_asns(request, self_handle):
    return download_csv(request, self_handle, 'asns')

def download_roas(request, self_handle):
    return download_csv(request, self_handle, 'roas')

def download_prefixes(request, self_handle):
    return download_csv(request, self_handle, 'prefixes')

def save_to_inbox(conf, request_type, content):
    """
    Save an incoming request from a client to the incoming mailbox
    for processing by a human.
    """

    user = conf.owner.all()[0]
    filename = request_type + '.xml'

    msg = email.message.Message()
    msg['Date'] = email.utils.formatdate()
    msg['From'] = '"%s" <%s>' % (conf.handle, user.email)
    msg['Message-ID'] = email.utils.make_msgid()
    msg['Subject'] = '%s for %s' % (filename, conf.handle)
    msg['X-rpki-self-handle'] = conf.handle
    msg['X-rpki-type'] = request_type
    msg.add_header('Content-Disposition', 'attachment', filename=filename)
    msg.set_type('application/x-rpki-setup')
    msg.set_payload(content)

    box = mailbox.Maildir(settings.INBOX)
    box.add(msg)
    box.close()

    return http.HttpResponse()

def get_response(conf, request_type):
    """
    If there is cached response for the given request type, simply
    return it.  Otherwise, look in the outbox mailbox for a response.
    """
    filename = glue.conf(conf.handle) + '/' + request_type + '.xml'
    if not os.path.exists(filename):
        box = mailbox.Maildir(settings.OUTBOX, factory=None)
        for key, msg in box.iteritems():
            # look for parent responses for this child
            if msg.get('x-rpki-type') == request_type and msg.get('x-rpki-self-handle') == conf.handle:
                with open(filename, 'w') as f:
                    f.write(msg.get_payload())
                break
        else:
            return http.HttpResponse('no response found', status=503)

        box.remove(key) # remove the msg from the outbox

    return serve_file(conf.handle, request_type + '.xml', 'application/xml')
 
@my_login_required
def parent_request(request, self_handle):
    conf = handle_or_404(request, self_handle)

    if request.method == 'POST':
        return save_to_inbox(conf, 'identity', request.POST['content'])
    else:
        return get_response(conf, 'parent')

@my_login_required
def repository_request(request, self_handle):
    conf = handle_or_404(request, self_handle)

    if request.method == 'POST':
        return save_to_inbox(conf, 'repository', request.POST['content'])
    else:
        return get_response(conf, 'repository')

@my_login_required
def myrpki_xml(request, self_handle):
    """
    Handles POST of the myrpki.xml file for a given resource handle.
    As a special case for resource handles hosted by APNIC, stash a
    copy of the first xml message in the rpki inbox mailbox as this
    will be required to complete the parent-child setup.
    """
    conf = handle_or_404(request, self_handle)

    if request.method == 'POST':
        fname = glue.conf(self_handle) + '/myrpki.xml'

        if not os.path.exists(fname):
            sys.stderr.write('Saving a copy of myrpki.xml for handle %s to inbox\n' % conf.handle)
            save_to_inbox(conf, 'myrpki', request.POST['content'])

        sys.stderr.write('writing %s\n' % fname)

        with open(fname, 'w') as myrpki_xml :
            myrpki_xml.write(request.POST['content'])

        glue.configure_daemons(conf.host)

    return serve_file(self_handle, 'myrpki.xml', 'application/xml')

def login(request):
    """
    A version of django.contrib.auth.views.login that will return an
    error response when the user/password is bad.  This is needed for
    use with rpkidemo to properly detect errors.  The django login
    view will return 200 with the login page when the login fails,
    which is not desirable when using rpkidemo.
    """

    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        user = auth.authenticate(username=username, password=password)
        if user is not None and user.is_active:
            auth.login(request, user)
            return http.HttpResponse('<p>login succeeded</p>')
        sys.stderr.write('failed login attempt for user %s\n' % username)
        return http.HttpResponseForbidden('<p>bad username or password</p>')
    else:
        return http.HttpResponse('<p>This should never been seen by a human</p>')

@handle_required
def roa_request_view(request, pk):
    """not yet implemented"""
    return

@handle_required
def roa_view(request, pk):
    """not yet implemented"""
    return

# vim:sw=4 ts=8 expandtab
