"""
    Tor2web
    Copyright (C) 2012 Hermes No Profit Association - GlobaLeaks Project

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

"""

:mod:`Tor2Web`
=====================================================

.. automodule:: Tor2Web
   :synopsis: SSL/TLS Hacks

.. moduleauthor:: Arturo Filasto' <art@globaleaks.org>
.. moduleauthor:: Giovanni Pellerano <evilaliv3@globaleaks.org>

"""

# -*- coding: utf-8 -*-
import glob

import os
from OpenSSL import SSL
from OpenSSL.crypto import load_certificate, dump_certificate, FILETYPE_PEM
from pyasn1.type import univ, constraint, char, namedtype, tag
from pyasn1.codec.der.decoder import decode
from twisted.internet import ssl


certificateAuthorityMap = {}

for certFileName in glob.glob("/etc/ssl/certs/*.pem"):
    # There might be some dead symlinks in there, so let's make sure it's real.
    if os.path.exists(certFileName):
        data = open(certFileName).read()
        x509 = load_certificate(FILETYPE_PEM, data)
        digest = x509.digest('sha1')
        # Now, de-duplicate in case the same cert has multiple names.
        certificateAuthorityMap[digest] = x509

class GeneralName(univ.Choice):
    # We are only interested in dNSNames. We use a default handler to ignore
    # other types.
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('dNSName', char.IA5String().subtype(
                implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 2)
            )
        ),
    )

class GeneralNames(univ.SequenceOf):
    componentType = GeneralName()
    sizeSpec = univ.SequenceOf.sizeSpec + constraint.ValueSizeConstraint(1, 1024)

def altnames(cert):
    altnames = []
    for i in range(cert.get_extension_count()):
        ext = cert.get_extension(i)
        if ext.get_short_name() == "subjectAltName":
            dec = decode(ext.get_data(), asn1Spec=GeneralNames())
            for j in dec[0]:
                altnames.append(j[0].asOctets())
    return altnames

class T2WSSLContext(SSL.Context):

    def load_tmp_dh(self, dhfile):
        """
        Function overridden in order to enforce ECDH/PFS
        """

        from OpenSSL._util import (ffi as _ffi,
                                   lib as _lib)

        if not isinstance(dhfile, bytes):
            raise TypeError("dhfile must be a byte string")

        bio = _lib.BIO_new_file(dhfile, b"r")
        if bio == _ffi.NULL:
            _raise_current_error()
        bio = _ffi.gc(bio, _lib.BIO_free)

        dh = _lib.PEM_read_bio_DHparams(bio, _ffi.NULL, _ffi.NULL, _ffi.NULL)
        dh = _ffi.gc(dh, _lib.DH_free)
        _lib.SSL_CTX_set_tmp_dh(self._context, dh)

        ecdh = _lib.EC_KEY_new_by_curve_name(_lib.NID_X9_62_prime256v1)
        ecdh = _ffi.gc(ecdh, _lib.EC_KEY_free)
        _lib.SSL_CTX_set_tmp_ecdh(self._context, ecdh)


class T2WSSLContextFactory(ssl.ContextFactory):
    _context = None

    def __init__(self, privateKeyFileName, certificateChainFileName, dhFileName, cipherList):
        """
        @param privateKeyFileName: Name of a file containing a private key
        @param certificateChainFileName: Name of a file containing a certificate chain
        @param dhFileName: Name of a file containing diffie hellman parameters
        @param cipherList: The SSL cipher list selection to use
        """
        self.privateKeyFileName = privateKeyFileName
        self.certificateChainFileName = certificateChainFileName

        # as discussed on https://trac.torproject.org/projects/tor/ticket/11598
        # there is no way of enabling all TLS methods excluding SSL.
        # the problem lies in the fact that SSL.TLSv1_METHOD | SSL.TLSv1_1_METHOD | SSL.TLSv1_2_METHOD
        # is denied by OpenSSL.
        #
        # As spotted by nickm the only good solution right now is to enable SSL.SSLv23_METHOD then explicitly
        # use options: SSL_OP_NO_SSLv2 and SSL_OP_NO_SSLv3
        #
        # This trick make openssl consider valid all TLS methods.
        self.sslmethod = SSL.SSLv23_METHOD

        self.dhFileName = dhFileName
        self.cipherList = cipherList

        # Create a context object right now.  This is to force validation of
        # the given parameters so that errors are detected earlier rather
        # than later.
        self.cacheContext()

    def cacheContext(self):
        if self._context is None:
            ctx = SSL.Context(self.sslmethod)

            ctx.set_options(SSL.OP_CIPHER_SERVER_PREFERENCE |
                            SSL.OP_NO_SSLv2 |
                            SSL.OP_NO_SSLv3 |
                            SSL.OP_SINGLE_DH_USE |
                            SSL.OP_NO_COMPRESSION |
                            SSL.OP_NO_TICKET)

            ctx.set_mode(SSL.MODE_RELEASE_BUFFERS)

            ctx.use_certificate_chain_file(self.certificateChainFileName)
            ctx.use_privatekey_file(self.privateKeyFileName)
            ctx.set_cipher_list(self.cipherList)
            ctx.load_tmp_dh(self.dhFileName)
            self._context = ctx

    def getContext(self):
        """
        Return an SSL context.
        """
        return self._context


class HTTPSVerifyingContextFactory(ssl.ClientContextFactory):
    def __init__(self, hostname, verify_tofu=None):
        self.hostname = hostname
        self.verify_tofu = verify_tofu
        
        # read in T2WSSLContextFactory why this settings ends in enabling only TLS
        self.method = SSL.SSLv23_METHOD

    def getContext(self):
        ctx = self._contextFactory(self.method)

        # Disallow SSL! It's insecure!
        ctx.set_options(SSL.OP_NO_SSLv2)
        ctx.set_options(SSL.OP_NO_SSLv3)

        ctx.set_options(SSL.OP_SINGLE_DH_USE)

        # http://en.wikipedia.org/wiki/CRIME_(security_exploit)
        # https://twistedmatrix.com/trac/ticket/5487
        # SSL_OP_NO_COMPRESSION = 0x00020000L
        ctx.set_options(0x00020000)

        store = ctx.get_cert_store()
        for value in certificateAuthorityMap.values():
            store.add_cert(value)
        ctx.set_verify(SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, self.verifyCert)
        return ctx

    def verifyCert(self, connection, x509, errno, depth, preverifyOK):
        verify = preverifyOK

        if  depth == 0 and verify:
            cn = x509.get_subject().commonName

            if cn.startswith(b"*.") and self.hostname.split(b".")[1:] == cn.split(b".")[1:]:
                verify = True

            elif self.hostname == cn:
                verify = True

            elif self.hostname in altnames(x509):
                verify = True

            if verify and self.verify_tofu is not None:
                return self.verify_tofu(self.hostname, dump_certificate(FILETYPE_PEM, x509))

        return verify

CERTS_TOFU = {}
