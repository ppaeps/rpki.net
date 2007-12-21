# $Id$

import os, yaml, MySQLdb, subprocess, signal
import rpki.resource_set, rpki.sundial, rpki.x509, rpki.https

just_show      = True
debug          = True

irbe_name      = "testdb"
irbe_key       = None
irbe_certs     = None

irdb_db_pass   = "fnord"
rpki_db_pass   = "fnord"

max_engines    = 10
irdb_base_port = 4400
rpki_base_port = irdb_base_port + max_engines
root_port      = rpki_base_port + max_engines

rpki_sql       = open("../docs/rpki-db-schema.sql").read()
irdb_sql       = open("../docs/sample-irdb.sql").read()

prog_python    = "/usr/local/bin/python"
prog_rpkid     = "rpkid.py"
prog_irdbd     = "irbd.py"
prog_poke      = "testpoke.py"
prog_rootd     = "testroot.py"

def main():

  y = [y for y in yaml.safe_load_all(open("testdb2.yaml"))]

  db = allocation_db(y.pop(0))

  if just_show:

    db.dump()
    for delta in y:
      print "Applying delta %s\n" % delta
      db.apply_delta(delta)
      db.dump()

  else:

    # Construct biz keys and certs for this script to use

    setup_biz_cert_chain(irbe_name)
    irbe_key = rpki.x509.X509(PEM_file = irbe_name + "-EE.key")
    irbe_certs = rpki.x509.X509_chain(PEM_files = (irbe_name + "-EE.cer", irbe_name + "-CA.cer"))

    # Construct biz keys and certs for rpki.py and irdb.py instances.

    for a in db:
      a.setup_biz_certs()

    # Construct config files for rpkid.py and irdb.py instances

    for a in db.engines:
      a.setup_conf_file()

    # Initialize sql for rpki.py and irdb.py instances

    for a in db.engines:
      a.setup_sql(rpki_sql, irdb_sql)

    # Populate IRDB(s)

    for a in db.engines:
      a.sync_sql()

    # Start RPKI and IRDB instances

    for a in db.engines:
      a.run_daemons()

    # Create objects in RPKI engines

    for a in db.engines:
      a.create_rpki_objects()

    # Write YAML files for leaves

    for a in db.leaves:
      a.write_leaf_yaml()

    # 8: Start cycle:

    while True:

      # Run cron in all RPKI instances

      for a in db.engines:
        a.run_cron()

      # Run all YAML clients

      for a in db.leaves:
        a.run_yaml()

      # Make sure that everybody got what they were supposed to get
      # and that everything that was supposed to be published has been
      # published.  [Not written yet]

      pass

      # Read and apply next deltas from master YAML

      if y:
        db.apply_delta(y.pop(0))
      else:
        break

    # Clean up

    for a in db.engines:
      a.kill_daemons()

class allocation_db(list):

  def __init__(self, yaml):
    self.root = allocation(yaml, self)
    assert self.root.is_root()
    for a in self:
      if a.sia_base is None:
        a.sia_base = a.parent.sia_base + a.name + "/"
      if a.base.valid_until is None:
        a.base.valid_until = a.parent.base.valid_until
    self.root.closure()
    self.map = dict((a.name, a) for a in self)
    self.engines = [a for a in self if not a.is_leaf()]
    self.leaves = [a for a in self if a.is_leaf()]
    for i, a in zip(range(len(self.engines)), self.engines):
      a.set_engine_number(i)

  def apply_delta(self, delta):
    for d in delta:
      self.map[d["name"]].apply_delta(d)
    self.root.closure()

  def dump(self):
    for a in self:
      print a

class allocation(object):

  parent       = None
  irdb_db_name = None
  irdb_port    = None
  rpki_db_name = None
  rpki_port    = None

  def __init__(self, yaml, db, parent = None):
    db.append(self)
    self.name = yaml["name"]
    self.parent = parent
    self.kids = [allocation(k, db, self) for k in yaml.get("kids", ())]
    self.base = rpki.resource_set.resource_bag(
      as = rpki.resource_set.resource_set_as(yaml.get("asn")),
      v4 = rpki.resource_set.resource_set_ipv4(yaml.get("ipv4")),
      v6 = rpki.resource_set.resource_set_ipv6(yaml.get("ipv6")),
      valid_until = yaml.get("valid_until"))
    self.sia_base = yaml.get("sia_base")

  def closure(self):
    """Compute the transitive resource closure for one resource attribute."""
    resources = self.base
    for kid in self.kids:
      resources = resources.union(kid.closure())
    self.resources = resources
    return resources

  def apply_delta(self, yaml):
    for k,v in yaml.items():
      if k != "name":
        getattr(self, "apply_" + k)(v)

  def apply_add_as(self, text): self.base.as = self.base.as.union(rpki.resource_set.resource_set_as(text))
  def apply_add_v4(self, text): self.base.v4 = self.base.v4.union(rpki.resource_set.resource_set_ipv4(text))
  def apply_add_v6(self, text): self.base.v6 = self.base.v6.union(rpki.resource_set.resource_set_ipv6(text))
  def apply_sub_as(self, text): self.base.as = self.base.as.difference(rpki.resource_set.resource_set_as(text))
  def apply_sub_v4(self, text): self.base.v4 = self.base.v4.difference(rpki.resource_set.resource_set_ipv4(text))
  def apply_sub_v6(self, text): self.base.v6 = self.base.v6.difference(rpki.resource_set.resource_set_ipv6(text))
  def apply_valid_until(self, stamp): self.base.valid_until = stamp

  def __str__(self):
    s = self.name + "\n"
    if self.resources.as:       s += "  ASN: %s\n" % self.resources.as
    if self.resources.v4:       s += " IPv4: %s\n" % self.resources.v4
    if self.resources.v6:       s += " IPv6: %s\n" % self.resources.v6
    if self.kids:               s += " Kids: %s\n" % ", ".join(k.name for k in self.kids)
    if self.parent:             s += "   Up: %s\n" % self.parent.name
    if self.sia_base:           s += "  SIA: %s\n" % self.sia_base
    return s + "Until: %s\n" % self.resources.valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")

  def is_leaf(self): return not self.kids
  def is_root(self): return self.parent is None
  def is_twig(self): return self.parent is not None and self.kids

  def set_engine_number(self, n):
    if n > max_engines:
      raise RuntimeError, "You asked for %d rpki engine instances, maximum is %d, sorry" % (n, max_engines)
    self.irdb_db_name = "irdb%d" % n
    self.irdb_port    = irdb_base_port + n
    self.rpki_db_name = "rpki%d" % n
    self.rpki_port    = rpki_base_port + n

  def setup_biz_certs(self):
    for tag in ("RPKI", "IRDB"):
      setup_biz_cert_chain(self.name + "-" + tag)
    self.rpkid_ta = rpki.x509.X509(PEM_file = self.name + "-RPKI-TA.cer")

  def setup_conf_file(self):
    d = { "my_name"      : self.name,
          "irbe_name"    : irbe_name,
          "irdb_db_name" : self.irdb_db_name,
          "irdb_db_pass" : irdb_db_pass,
          "irdb_port"    : self.irdb_port,
          "rpki_db_name" : self.rpki_db_name,
          "rpki_db_pass" : rpki_db_pass,
          "rpki_port"    : self.rpki_port }
    s = conf_fmt_1 % d
    if debug:
      print "Would write config file " + self.name + ".conf containing:\n" + s
    else:
      f = open(self.name + ".conf", "w")
      f.write(s)
      f.close()

  def setup_sql(self, rpki_sql, irdb_sql):
    db = MySQLdb.connect(user = "rpki", db = self.rpki_db_name, passwd = rpki_db_pass)
    db.cursor().execute(rpki_sql)
    db.close()
    db = MySQLdb.connect(user = "irdb", db = self.irdb_db_name, passwd = irdb_db_pass)
    cur = db.cursor()
    cur.execute(irdb_sql)
    for kid in self.kids:
      cur.execute("INSERT registrant (IRBE_mapped_id, subject_name, valid_until) VALUES (%s, %s, %s)", (kid.name, kid.name, kid.valid_until))
    db.close()

  def sync_sql(self):
    db = MySQLdb.connect(user = "irdb", db = self.irdb_db_name, passwd = irdb_db_pass)
    cur = db.cursor()
    cur.execute("DELETE FROM asn")
    cur.execute("DELETE FROM net")
    for kid in self.kids:
      cur.execute("SELECT registrant_id FROM registrant WHERE IRBE_mapped_id = %s", kid.name)
      registrant_id = cur.fetchone()[0]
      for as_range in kid.as:
        cur.execute("INSERT asn (start_as, end_as, registrant_id) VALUES (%s, %s, %s)", (as_range.min, as_range.max, registrant_id))
      for v4_range in kid.v4:
        cur.execute("INSERT net (start_ip, end_ip, version, registrant_id) VALUES (%s, %s, 4, %s)", (as_v4.min, as_v4.max, registrant_id))
      for v6_range in kid.v6:
        cur.execute("INSERT net (start_ip, end_ip, version, registrant_id) VALUES (%s, %s, 6, %s)", (as_v6.min, as_v6.max, registrant_id))
    db.close()

  def run_daemons(self):
    self.rpkid_process = subprocess.Popen((prog_python, prog_rpkid, "-c", self.name + ".conf"))
    self.irdbd_process = subprocess.Popen((prog_python, prog_irdbd, "-c", self.name + ".conf"))

  def kill_daemons(self):
    for proc in (self.rpkid_process, self.irdbd_process):
      try:
        os.kill(proc.pid, signal.SIGTERM)
      except:
        pass
      proc.wait()

  def call_rpkid(self, pdu):
    pdu.type = "query"
    elt = rpki.left_right.msg((pdu,)).toXML()
    rpki.relaxng.left_right.assertValid(elt)
    cms = rpki.cms.xml_sign(
      elt           = elt,
      key           = irbe_key,
      certs         = irbe_certs)
    cms = rpki.https.client(
      privateKey    = irbe_key,
      certChain     = irbe_certs,
      x509TrustList = rpki.x509.X509_chain(self.rpkid_ta),
      url           = "https://localhost:%d/left-right" % self.rpki_port,
      msg           = cms)
    elt = rpki.cms.xml_verify(cms = cms, ta = self.rpkid_ta)
    rpki.relaxng.left_right.assertValid(elt)
    pdu = rpki.left_right.sax_handler.saxify(elt)[0]
    assert pdu.type == "reply" and not isinstance(pdu, rpki.left_right.report_error_elt)
    return pdu

  def create_rpki_objects(self):
    """Create RPKI engine objects for this engine.

    Parent and child objects are tricky:

    - Parent object needs to know child_id by which parent refers to
      this engine in order to set the contact URI correctly.

    - Child object needs to record the child_id by which this engine
      refers to the child.

    This all just works so long as we walk the set of engines in the
    right order (parents before their children).

    Root node of the engine tree is special, it too has a parent but
    that one is the magic self-signed micro engine.
    """

    self.self_id = self.call_rpkid(rpki.left_right.self_elt.make_pdu(action = "create", crl_interval = 84600)).self_id

    pdu = call_rpkid(rpki.left_right.bsc_elt.make_pdu(action = "create", self_id = self.self_id, generate_keypair = True))
    self.bsc_id = pdu.bsc_id

    cmd = ("openssl", "x509", "-req", "-CA", self.name + "-RPKI-CA.cer", "-CAkey", self.name + "-RPKI-CA.key", "-CAserial", self.name + "-RPKI-CA.srl")
    signer = subprocess.Popen(cmd, stdin = subprocess.PIPE, stdout = subprocess.PIPE)
    bsc_ee = rpki.x509.X509(PEM = signer.communicate(input = pdu.pkcs10_cert_request.get_PEM())[0])

    self.call_rpkid(rpki.left_right.bsc_elt.make_pdu(action = "set", self_id = self.self_id, bsc_id = self.bsc_id,
                                                     signing_cert = [bsc_ee, rpki.x509.X509(PEM_file = self.name + "-RPKI-CA.cer")]))

    self.repository_id = self.call_rpkid(rpki.left_right.repository_elt.make_pdu(action = "create", self_id = self.self_id, bsc_id = self.bsc_id)).repository_id

    if self.parent is None:
      self.parent_id = self.call_rpkid(rpki.left_right.parent_elt.make_pdu(
        action = "create", self_id = self.self_id, bsc_id = self.bsc_id, repository_id = self.repository_id, sia_base = self.sia_base,
        cms_ta = root_ta, https_ta = root_ta, peer_contact_uri = root_uri)).parent_id
    else:
      self.parent_id = self.call_rpkid(rpki.left_right.parent_elt.make_pdu(
        action = "create", self_id = self.self_id, bsc_id = self.bsc_id, repository_id = self.repository_id, sia_base = self.sia_base,
        cms_ta = self.parent.rpkid_ta, https_ta = self.parent.rpkid_ta,
        peer_contact_uri = "https://localhost:%s/up-down/%s" % (self.parent.rpki_port, self.child_id))).parent_id

    for kid in self.kids:
      kid.child_id = self.call_rpkid(rpki.left_right.child_elt.make_pdu(action = "create", self_id = self.self_id, bsc_id = self.bsc_id, cms_ta = kid.rpkid_ta)).child_id

  def write_leaf_yaml(self):
    """Write YAML scripts for leaf nodes.  Only supports list requests
    at the moment: issue requests would require class and SIA values,
    revoke requests would require class and SKI values.
    """

    f = open(self.name + ".yaml", "w")
    f.write(yaml_fmt_1 % {
      child_id    : self.child_id,
      parent_name : self.parent.name,
      my_name     : self.name,
      https_port  : self.parent.rpki_port })
    f.close()

  def run_cron(self):
    """Trigger cron run for this engine."""
    rpki.https.client(privateKey      = irbe_key,
                      certChain       = irbe_certs,
                      x509TrustList   = rpki.x509.X509_chain(self.rpkid_ta),
                      url             = "https://localhost:%d/cronjob" % self.rpki_port,
                      msg             = "Run cron now, please")

  def run_yaml(self):
    pass

def setup_biz_cert_chain(name):
  s = ""
  for kind in ("EE", "CA", "TA"):
    n = "%s-%s" % (name, kind)
    c = biz_cert_fmt_1 % (n, "true" if kind in ("CA", "TA") else "false")
    if debug:
      print "Would write config file " + n + ".cnf containing:\n\n" + c
    else:
      f = open("%s.cnf" % n, "w")
      f.write(c)
      f.close()
    if not os.path.exists(n + ".key") or not os.path.exists(n + ".req"):
      s += biz_cert_fmt_2 % ((n,) * 3)
  s += biz_cert_fmt_3 % ((name,) * 14)
  if debug:
    print "Would execute:\n\n" + s
  else:
    subprocess.check_call(s, shell=True)

biz_cert_fmt_1 = '''\
[ req ]
distinguished_name	= req_dn
x509_extensions		= req_x509_ext
prompt			= no
default_md		= sha256

[ req_dn ]
CN			= Test Certificate %s

[ req_x509_ext ]
basicConstraints	= CA:%s
subjectKeyIdentifier	= hash
authorityKeyIdentifier	= keyid:always
'''

biz_cert_fmt_2 = '''\
openssl req -new -newkey rsa:2048 -nodes -keyout %s.key -out %s.req -config %s.cnf &&
'''

biz_cert_fmt_3 = '''\
openssl x509 -req -in %s-TA.req -out %s-TA.cer -extfile %s-TA.cnf -extensions req_x509_ext -signkey %s-TA.key -days 60 &&
openssl x509 -req -in %s-CA.req -out %s-CA.cer -extfile %s-CA.cnf -extensions req_x509_ext -CA %s-TA.cer -CAkey %s-TA.key -CAcreateserial &&
openssl x509 -req -in %s-EE.req -out %s-EE.cer -extfile %s-EE.cnf -extensions req_x509_ext -CA %s-CA.cer -CAkey %s-CA.key -CAcreateserial
'''

poke_yaml_fmt_1 = '''---
version:                1
posturl:                https://localhost:%(https_port)s/up-down/%(child_id)s
recipient-id:           "%(parent_name)s"
sender-id:              "%(my_name)s"

cms-cert-file:          %(my_name)s-EE.cer
cms-key-file:           %(my_name)s-EE.key
cms-ca-cert-file:       %(parent_name)s-Root.cer
cms-cert-chain-file:    [ %(my_name)s-CA.cer ]

ssl-cert-file:          %(my_name)s-EE.cer
ssl-key-file:           %(my_name)s-EE.key
ssl-ca-cert-file:       %(parent_name)s-Root.cer

requests:
  list:
    type:               list
'''

conf_fmt_1 = '''\

[rpkid]

sql-database	= %(rpki_db_name)s
sql-username	= rpki
sql-password	= %(rpki_db_pass)s

cms-key		= %(my_name)s-RPKI-EE.key
cms-cert.0	= %(my_name)s-RPKI-EE.cer
cms-cert.1	= %(my_name)s-RPKI-CA.cer

cms-ta-irdb	= %(my_name)s-IRDB-TA.cer
cms-ta-irbe	= %(irbe_name)s-TA.cer

https-key	= %(my_name)s-RPKI-EE.key
https-cert.0	= %(my_name)s-RPKI-EE.cer
https-cert.1	= %(my_name)s-RPKI-CA.cer

https-ta	= %(irbe_name)s-TA.cer

irdb-url	= https://localhost:%(irdb_port)d/

https-server-port = %(rpki_port)d

[irdb]

sql-database	= %(irdb_db_name)s
sql-username	= irdb
sql-password	= %(irdb_db_pass)s

cms-key		= %(my_name)s-IRDB-EE.key
cms-cert.0	= %(my_name)s-IRDB-EE.cer
cms-cert.1	= %(my_name)s-IRDB-CA.cer
cms-ta		= %(my_name)s-RPKI-TA.cer

https-key	= %(my_name)s-IRDB-EE.key
https-cert.0	= %(my_name)s-IRDB-EE.cer
https-cert.1	= %(my_name)s-IRDB-CA.cer
https-ta.0	= %(irbe_name)s-TA.cer
https-ta.1	= %(my_name)s-RPKI-TA.cer

https-url	= https://localhost:%(irdb_port)d/

[irbe-cli]

cms-key		= %(irbe_name)s-EE.key
cms-cert.0	= %(irbe_name)s-EE.cer
cms-cert.1	= %(irbe_name)s-CA.cer
cms-ta		= %(my_name)s-RPKI-TA.cer

https-key	= %(irbe_name)s-EE.key
https-cert.0	= %(irbe_name)s-EE.cer
https-cert.1	= %(irbe_name)s-CA.cer
https-ta.0	= %(my_name)s-RPKI-TA.cer
https-ta.1	= %(my_name)s-IRDB-TA.cer

https-url	= https://localhost:%(rpki_port)d/left-right
'''

main()
