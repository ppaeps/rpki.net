# $Id$

import re, ipaddrs

class resource_range(object):
  """
  Generic resource range type.  Assumes underlying type is some kind of integer.
  You probably don't want to use this type directly.
  """

  def __init__(self, min, max):
    assert min <= max, "Mis-ordered range: %s before %s" % (str(min), str(max))
    self.min = min
    self.max = max

  def __cmp__(self, other):
    c = self.min - other.min
    if c == 0: c = self.max - other.max
    if c < 0:  c = -1
    if c > 0:  c =  1
    return c

class resource_range_as(resource_range):
  """
  Range of Autonomous System Numbers.
  Denote a single ASN by a range whose min and max values are identical.
  """

  def __str__(self):
    if self.min == self.max:
      return str(self.min)
    else:
      return str(self.min) + "-" + str(self.max)

class resource_range_ip(resource_range):
  """
  Range of (generic) IP addresses.  Prefixes are converted to ranges
  on input, and ranges that can be represented as prefixes are written
  as prefixes on output.
  """

  def __str__(self):
    mask = self.min ^ self.max
    prefixlen = self.min.bits
    while mask & 1:
      prefixlen -= 1
      mask >>= 1
    if mask:
      return str(self.min) + "-" + str(self.max)
    else:
      return str(self.min) + "/" + str(prefixlen)

class resource_range_ipv4(resource_range_ip):
  """
  Range of IPv4 addresses.
  """
  pass

class resource_range_ipv6(resource_range_ip):
  """
  Range of IPv6 addresses.
  """
  pass

def rsplit(rset, that):
  this = rset.pop(0)
  cell_type = type(this.min)
  assert type(this) is type(that) and type(this.max) is cell_type and type(that.min) is cell_type and type(that.max) is cell_type
  if this.min < that.min:
    rset.insert(0, type(this)(this.min, cell_type(that.min - 1)))
    rset.insert(1, type(this)(that.min, this.max))
  else:
    assert this.max > that.max
    rset.insert(0, type(this)(this.min, that.max))
    rset.insert(1, type(this)(cell_type(that.max + 1), this.max))

class resource_set(list):
  """
  Generic resource set.  List type containing resource ranges.  You
  probably don't want to use this type directly.
  """

  def __init__(self, ini=None):
    if isinstance(ini, str) and len(ini):
      self.extend(map(self.parse_str, ini.split(",")))
    elif isinstance(ini, tuple):
      self.parse_tuple(ini)
    elif isinstance(ini, list):
      self.extend(ini)
    else:
      assert ini is None
    self.sort()
    if __debug__:
      for i in range(0, len(self) - 1):
        assert self[i].max < self[i+1].min, "Resource overlap: %s %s" % (self[i], self[i+1])

  def __str__(self):
    return ",".join(map(str, self))

  def comm(self, other):
    """
    Like comm(1), sort of.  Returns a tuple of three resource sets:
    resources only in self, resources only in other, and resources in
    both.  Used (not very efficiently) as the basis for most set
    operations on resource sets.
    """
    assert type(self) is type(other)
    set1 = self[:]
    set2 = other[:]
    only1, only2, both = [], [], []
    while set1 or set2:
      if set1 and (not set2 or set1[0].max < set2[0].min):
        only1.append(set1.pop(0))
      elif set2 and (not set1 or set2[0].max < set1[0].min):
        only2.append(set2.pop(0))
      elif set1[0].min < set2[0].min:
        rsplit(set1, set2[0])
      elif set2[0].min < set1[0].min:
        rsplit(set2, set1[0])
      elif set1[0].max < set2[0].max:
        rsplit(set2, set1[0])
      elif set2[0].max < set1[0].max:
        rsplit(set1, set2[0])
      else:
        assert set1[0].min == set2[0].min and set1[0].max == set2[0].max
        both.append(set1.pop(0))
        set2.pop(0)
    return type(self)(only1), type(self)(only2), type(self)(both)

  def union(self, other):
    """
    Set union for resource sets.
    """
    assert type(self) is type(other)
    set1 = self[:]
    set2 = other[:]
    result = []
    while set1 or set2:
      if set1 and (not set2 or set1[0].max < set2[0].min):
        result.append(set1.pop(0))
      elif set2 and (not set1 or set2[0].max < set1[0].min):
        result.append(set2.pop(0))
      else:
        this = set1.pop(0)
        that = set2.pop(0)
        assert type(this) is type(that)
        if this.min < that.min: min = this.min
        else:                   min = that.min
        if this.max > that.max: max = this.max
        else:                   max = that.max
        result.append(type(this)(min, max))
    return type(self)(result)

  def intersection(self, other):
    """
    Set intersection for resource sets.
    """
    return self.comm(other)[2]

  def difference(self, other):
    """
    Set difference for resource sets.
    """
    return self.comm(other)[0]

  def symmetric_difference(self, other):
    """
    Set symmetric difference (XOR) for resource sets.
    """
    com = self.comm(other)
    return com[0].union(com[1])

  def contains(self, item):
    """
    Set membership test for resource sets.
    """
    for i in self:
      if isinstance(item, type(i)) and i.min <= item.min and i.max >= item.max:
        return True
      elif isinstance(item, type(i.min)) and i.min <= item and i.max >= item:
        return True
      else:
        assert isinstance(item, (type(i), type(i.min)))
    return False

class resource_set_as(resource_set):
  """
  ASN resource set.
  """

  def parse_str(self, x):
    r = re.match("^([0-9]+)-([0-9]+)$", x)
    if r:
      return resource_range_as(long(r.group(1)), long(r.group(2)))
    else:
      return resource_range_as(long(x), long(x))

  def parse_tuple(self, x):
    assert x[0] == "asIdsOrRanges"      # Not handling "inherit" yet
    for aor in x[1]:
      if aor[0] == "range":
        min = aor[1][0]
        max = aor[1][1]
      else:
        min = aor[1]
        max = min
      self.append(resource_range_as(min, max))

class resource_set_ip(resource_set):
  """
  (Generic) IP address resource set.
  You probably don't want to use this type directly.
  """

  def parse_str(self, x):
    r = re.match("^([0-9:.a-fA-F]+)-([0-9:.a-fA-F]+)$", x)
    if r:
      return self.range_type(self.addr_type(r.group(1)), self.addr_type(r.group(2)))
    r = re.match("^([0-9:.a-fA-F]+)/([0-9]+)$", x)
    if r:
      min = self.addr_type(r.group(1))
      prefixlen = int(r.group(2))
      mask = (1 << (self.addr_type.bits - prefixlen)) - 1
      assert (min & mask) == 0, "Resource not in canonical form: %s" % (x)
      max = self.addr_type(min | mask)
      return self.range_type(min, max)
    raise RuntimeError, 'Bad IP resource "%s"' % (x)

  def parse_tuple(self, x):
    assert x[0] == "addressesOrRanges"  # Not handling "inherit" yet
    for aor in x[1]:
      if aor[0] == "addressRange":
        min = bs2long(aor[1][0]) << (self.addr_type.bits - len(aor[1][0]))
        max = bs2long(aor[1][1]) << (self.addr_type.bits - len(aor[1][1]))
        mask = (1L << (self.addr_type.bits - len(aor[1][1]))) - 1
      else:
        min = bs2long(aor[1]) << (self.addr_type.bits - len(aor[1]))
        mask = (1L << (self.addr_type.bits - len(aor[1]))) - 1
        assert (min & mask) == 0, "Resource not in canonical form: %s" % (str(x))
      max = min | mask
      self.append(self.range_type(self.addr_type(min), self.addr_type(max)))

class resource_set_ipv4(resource_set_ip):
  """
  IPv4 address resource set.
  """

  addr_type = ipaddrs.v4addr
  range_type = resource_range_ipv4

class resource_set_ipv6(resource_set_ip):
  """
  IPv6 address resource set.
  """

  addr_type = ipaddrs.v6addr
  range_type = resource_range_ipv6

def bs2long(bs):
  """
  Convert a bitstring (tuple representation) into a long.
  """

  return reduce(lambda x, y: (x << 1) | y, bs, 0L)

def parse_extensions(exts):
  """
  Parse RFC 3779 extensions out of the tuple encoding returned by
  POW.pkix.cert.getExtensions().
  """

  as = None
  v4 = None
  v6 = None

  for x in exts:
    if x[0] == (1, 3, 6, 1, 5, 5, 7, 1, 8): # sbgp-autonomousSysNum
      assert x[2][1] is None, "RDI not implemented: %s" % (str(x))
      assert as is None
      as = resource_set_as(x[2][0])
    if x[0] == (1, 3, 6, 1, 5, 5, 7, 1, 7): # sbgp-ipAddrBlock
      for fam in x[2]:
        if fam[0] == "\x00\x01":
          assert v4 is None
          v4 = resource_set_ipv4(fam[1])
        if fam[0] == "\x00\x02":
          assert v6 is None
          v6 = resource_set_ipv6(fam[1])
  return as, v4, v6

# Test suite for set operations.  This will probably go away eventually

if __name__ == "__main__":

  def test(t, s1, s2):
    print
    r1 = t(s1)
    r2 = t(s2)
    print "x:  ", r1
    print "y:  ", r2
    v1 = r1.comm(r2)
    v2 = r2.comm(r1)
    assert v1[0] == v2[1] and v1[1] == v2[0] and v1[2] == v2[2]
    for i in r1: assert r1.contains(i) and r1.contains(i.min) and r1.contains(i.max)
    for i in r2: assert r2.contains(i) and r2.contains(i.min) and r2.contains(i.max)
    for i in v1[0]: assert r1.contains(i) and not r2.contains(i)
    for i in v1[1]: assert not r1.contains(i) and r2.contains(i)
    for i in v1[2]: assert r1.contains(i) and r2.contains(i)
    v1 = r1.union(r2)
    v2 = r2.union(r1)
    assert v1 == v2
    print "x|y:", v1
    v1 = r1.difference(r2)
    v2 = r2.difference(r1)
    print "x-y:", v1
    print "y-x:", v2
    v1 = r1.symmetric_difference(r2)
    v2 = r2.symmetric_difference(r1)
    assert v1 == v2
    print "x^y:", v1
    v1 = r1.intersection(r2)
    v2 = r2.intersection(r1)
    assert v1 == v2
    print "x&y:", v1

  print "Testing set operations on resource sets"
  test(resource_set_as, "1,2,3,4,5,6,11,12,13,14,15", "1,2,3,4,5,6,111,121,131,141,151")
  test(resource_set_ipv4, "10.0.0.44/32,10.6.0.2/32", "10.3.0.0/24,10.0.0.77/32")
  test(resource_set_ipv4, "10.0.0.44/32,10.6.0.2/32", "10.0.0.0/24")
  test(resource_set_ipv4, "10.0.0.0/24", "10.3.0.0/24,10.0.0.77/32")
