# vim: set fileencoding=utf-8 :
import base64
import copy
import email.header
import email.parser
import email.utils
import errno
import lxml.html
import mailbox
import mimetypes
import os
import quopri
import random
import re
import StringIO
import threading
import traceback
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from lxml.html.clean import Cleaner
from mailpile.util import *
from platform import system
from urllib import quote, unquote
from datetime import datetime, timedelta

from mailpile.crypto.gpgi import GnuPG
from mailpile.crypto.gpgi import OpenPGPMimeSigningWrapper
from mailpile.crypto.gpgi import OpenPGPMimeEncryptingWrapper
from mailpile.crypto.gpgi import OpenPGPMimeSignEncryptWrapper
from mailpile.crypto.mime import UnwrapMimeCrypto, MessageAsString
from mailpile.crypto.state import EncryptionInfo, SignatureInfo
from mailpile.i18n import gettext as _
from mailpile.i18n import ngettext as _n
from mailpile.mail_generator import Generator
from mailpile.vcard import AddressInfo


MBX_ID_LEN = 4  # 4x36 == 1.6 million mailboxes


def FormatMbxId(n):
    if not isinstance(n, (str, unicode)):
        n = b36(n)
    if len(n) > MBX_ID_LEN:
        raise ValueError(_('%s is too large to be a mailbox ID') % n)
    return ('0000' + n).lower()[-MBX_ID_LEN:]


class NotEditableError(ValueError):
    pass


class NoFromAddressError(ValueError):
    pass


class NoRecipientError(ValueError):
    pass


class InsecureSmtpError(ValueError):
    pass


class NoSuchMailboxError(OSError):
    pass


GLOBAL_CONTENT_ID_LOCK = MboxLock()
GLOBAL_CONTENT_ID = random.randint(0, 0xfffffff)

def MakeContentID():
    global GLOBAL_CONTENT_ID
    with GLOBAL_CONTENT_ID_LOCK:
        GLOBAL_CONTENT_ID += 1
        GLOBAL_CONTENT_ID %= 0xfffffff
        return '%x' % GLOBAL_CONTENT_ID


GLOBAL_PARSE_CACHE_LOCK = MboxLock()
GLOBAL_PARSE_CACHE = []

def ClearParseCache(cache_id=None, pgpmime=False, full=False):
    global GLOBAL_PARSE_CACHE
    with GLOBAL_PARSE_CACHE_LOCK:
        GPC = GLOBAL_PARSE_CACHE
        for i in range(0, len(GPC)):
            if (full or
                    (pgpmime and GPC[i][1]) or
                    (cache_id and GPC[i][0] == cache_id)):
                GPC[i] = (None, None, None)


def ParseMessage(fd, cache_id=None, update_cache=False,
                     pgpmime=True, config=None):
    global GLOBAL_PARSE_CACHE
    if not GnuPG:
        pgpmime = False

    if cache_id is not None and not update_cache:
        with GLOBAL_PARSE_CACHE_LOCK:
            for cid, pm, message in GLOBAL_PARSE_CACHE:
                if cid == cache_id and pm == pgpmime:
                    return message

    if pgpmime:
        message = ParseMessage(fd, cache_id=cache_id,
                               pgpmime=False,
                               config=config)
        if message is None:
            return None
        if cache_id is not None:
            # Caching is enabled, let's not clobber the encrypted version
            # of this message with a fancy decrypted one.
            message = copy.deepcopy(message)
        def MakeGnuPG(*args, **kwargs):
            return GnuPG(config, *args, **kwargs)
        UnwrapMimeCrypto(message, protocols={
            'openpgp': MakeGnuPG
        })
    else:
        try:
            if not hasattr(fd, 'read'):  # Not a file, is it a function?
                fd = fd()
            assert(hasattr(fd, 'read'))
        except (TypeError, AssertionError):
            return None

        message = email.parser.Parser().parse(fd)
        msi = message.signature_info = SignatureInfo(bubbly=False)
        mei = message.encryption_info = EncryptionInfo(bubbly=False)
        for part in message.walk():
            part.signature_info = SignatureInfo(parent=msi)
            part.encryption_info = EncryptionInfo(parent=mei)

    if cache_id is not None:
        with GLOBAL_PARSE_CACHE_LOCK:
            # Keep 25 items, put new ones at the front
            GLOBAL_PARSE_CACHE[24:] = []
            GLOBAL_PARSE_CACHE[:0] = [(cache_id, pgpmime, message)]

    return message


def ExtractEmails(string, strip_keys=True):
    emails = []
    startcrap = re.compile('^[\'\"<(]')
    endcrap = re.compile('[\'\">);]$')
    string = string.replace('<', ' <').replace('(', ' (')
    for w in [sw.strip() for sw in re.compile('[,\s]+').split(string)]:
        atpos = w.find('@')
        if atpos >= 0:
            while startcrap.search(w):
                w = w[1:]
            while endcrap.search(w):
                w = w[:-1]
            if strip_keys and '#' in w[atpos:]:
                w = w[:atpos] + w[atpos:].split('#', 1)[0]
            # E-mail addresses are only allowed to contain ASCII
            # characters, so we just strip everything else away.
            emails.append(CleanText(w,
                                    banned=CleanText.WHITESPACE,
                                    replace='_').clean)
    return emails


def ExtractEmailAndName(string):
    email = (ExtractEmails(string) or [''])[0]
    name = (string
            .replace(email, '')
            .replace('<>', '')
            .replace('"', '')
            .replace('(', '')
            .replace(')', '')).strip()
    return email, (name or email)


def CleanMessage(config, msg):
    replacements = []
    for key, value in msg.items():
        lkey = key.lower()

        # Remove headers we don't want to expose
        if (lkey.startswith('x-mp-internal-') or
                lkey in ('bcc', 'encryption', 'attach-pgp-pubkey')):
            replacements.append((key, None))

        # Strip the #key part off any e-mail addresses:
        elif lkey in ('to', 'from', 'cc'):
            if '#' in value:
                replacements.append((key, re.sub(
                    r'(@[^<>\s#]+)#[a-fxA-F0-9]+([>,\s]|$)', r'\1\2', value)))

    for key, val in replacements:
        del msg[key]
    for key, val in replacements:
        if val:
            msg[key] = val

    return msg


def PrepareMessage(config, msg, sender=None, rcpts=None, events=None):
    msg = copy.deepcopy(msg)

    # Short circuit if this message has already been prepared.
    if 'x-mp-internal-sender' in msg and 'x-mp-internal-rcpts' in msg:
        return (sender or msg['x-mp-internal-sender'],
                rcpts or [r.strip()
                          for r in msg['x-mp-internal-rcpts'].split(',')],
                msg,
                events)

    attach_pgp_pubkey = 'no'
    attached_pubkey = False
    crypto_policy = config.prefs.crypto_policy.lower()
    rcpts = rcpts or []

    # Iterate through headers to figure out what we want to do...
    need_rcpts = not rcpts
    for hdr, val in msg.items():
        lhdr = hdr.lower()
        if lhdr == 'from':
            sender = sender or val
        elif lhdr == 'encryption':
            crypto_policy = val.lower()
        elif lhdr == 'attach-pgp-pubkey':
            attach_pgp_pubkey = val.lower()
        elif need_rcpts and lhdr in ('to', 'cc', 'bcc'):
            rcpts += ExtractEmails(val, strip_keys=False)

    # Are we sane?
    if not sender:
        raise NoFromAddressError()
    if not rcpts:
        raise NoRecipientError()

    # Are we encrypting? Signing?
    if crypto_policy == 'default':
        crypto_policy = config.prefs.crypto_policy

    # This is the BCC hack that Brennan hates!
    if config.prefs.always_bcc_self:
        rcpts += [sender]

    sender = ExtractEmails(sender, strip_keys=False)[0]
    sender_keyid = None
    if config.prefs.openpgp_header:
        try:
            gnupg = GnuPG(config)
            seckeys = dict([(uid["email"], fp) for fp, key
                            in gnupg.list_secret_keys().iteritems()
                            if key["capabilities_map"].get("encrypt")
                            and key["capabilities_map"].get("sign")
                            for uid in key["uids"]])
            sender_keyid = seckeys.get(sender)
        except (KeyError, TypeError, IndexError, ValueError):
            traceback.print_exc()

    rcpts, rr = [sender], rcpts
    for r in rr:
        for e in ExtractEmails(r, strip_keys=False):
            if e not in rcpts:
                rcpts.append(e)

    # Add headers we require
    if 'date' not in msg:
        msg['Date'] = email.utils.formatdate()

    if sender_keyid and config.prefs.openpgp_header:
        msg["OpenPGP"] = "id=%s; preference=%s" % (sender_keyid,
                                                   config.prefs.openpgp_header)

    # Do we want to attach a key to outgoing messages?
    if attach_pgp_pubkey in ['yes', 'true']:
        g = GnuPG(config)
        keys = g.address_to_keys(ExtractEmails(sender)[0])
        for fp, key in keys.iteritems():
            if not any(key["capabilities_map"].values()):
                continue
            # We should never really hit this more than once. But if we do, it
            # should still be fine.
            keyid = key["keyid"]
            data = g.get_pubkey(keyid)

            try:
                from_name = key["uids"][0]["name"]
                filename = _('Encryption key for %s.asc') % from_name
            except:
                filename = _('My encryption key.asc')

            att = MIMEBase('application', 'pgp-keys')
            att.set_payload(data)
            encoders.encode_base64(att)
            del att['MIME-Version']
            att.add_header('Content-Id', MakeContentID())
            att.add_header('Content-Disposition', 'attachment',
                           filename=filename)
            att.signature_info = SignatureInfo(parent=msg.signature_info)
            att.encryption_info = EncryptionInfo(parent=msg.encryption_info)
            msg.attach(att)
            attached_pubkey = True

    # Should be 'openpgp', but there is no point in being precise
    if 'pgp' in crypto_policy or 'gpg' in crypto_policy:
        wrapper = None
        if 'sign' in crypto_policy and 'encrypt' in crypto_policy:
            wrapper = OpenPGPMimeSignEncryptWrapper
        elif 'sign' in crypto_policy:
            wrapper = OpenPGPMimeSigningWrapper
        elif 'encrypt' in crypto_policy:
            wrapper = OpenPGPMimeEncryptingWrapper
        elif 'none' not in crypto_policy:
            raise ValueError(_('Unknown crypto policy: %s') % crypto_policy)
        if wrapper:
            cpi = config.prefs.inline_pgp
            msg = wrapper(config,
                          sender=sender,
                          cleaner=lambda m: CleanMessage(config, m),
                          recipients=rcpts
                          ).wrap(msg, prefer_inline=cpi)
    elif crypto_policy and crypto_policy != 'none':
        raise ValueError(_('Unknown crypto policy: %s') % crypto_policy)

    rcpts = set([r.rsplit('#', 1)[0] for r in rcpts])
    if attached_pubkey:
        msg['x-mp-internal-pubkeys-attached'] = "Yes"
    msg['x-mp-internal-readonly'] = str(int(time.time()))
    msg['x-mp-internal-sender'] = sender
    msg['x-mp-internal-rcpts'] = ', '.join(rcpts)
    return (sender, rcpts, msg, events)


MUA_HEADERS = ('date', 'from', 'to', 'cc', 'subject', 'message-id', 'reply-to',
               'mime-version', 'content-disposition', 'content-type',
               'user-agent', 'list-id', 'list-subscribe', 'list-unsubscribe',
               'x-ms-tnef-correlator', 'x-ms-has-attach')
DULL_HEADERS = ('in-reply-to', 'references')


def HeaderPrintHeaders(message):
    """Extract message headers which identify the MUA."""
    headers = [k for k, v in message.items()]

    # The idea here, is that MTAs will probably either prepend or append
    # headers, not insert them in the middle. So we strip things off the
    # top and bottom of the header until we see something we are pretty
    # comes from the MUA itself.
    while headers and headers[0].lower() not in MUA_HEADERS:
        headers.pop(0)
    while headers and headers[-1].lower() not in MUA_HEADERS:
        headers.pop(-1)

    # Finally, we return the "non-dull" headers, the ones we think will
    # uniquely identify this particular mailer and won't vary too much
    # from message-to-message.
    return [h for h in headers if h.lower() not in DULL_HEADERS]


def HeaderPrint(message):
    """Generate a fingerprint from message headers which identifies the MUA."""
    return b64w(sha1b64('\n'.join(HeaderPrintHeaders(message)))).lower()


class Email(object):
    """This is a lazy-loading object representing a single email."""

    def __init__(self, idx, msg_idx_pos,
                 msg_parsed=None, msg_parsed_pgpmime=None,
                 msg_info=None, ephemeral_mid=None):
        self.index = idx
        self.config = idx.config
        self.msg_idx_pos = msg_idx_pos
        self.ephemeral_mid = ephemeral_mid
        self.reset_caches(msg_parsed=msg_parsed,
                          msg_parsed_pgpmime=msg_parsed_pgpmime,
                          msg_info=msg_info,
                          clear_parse_cache=False)

    def msg_mid(self):
        return self.ephemeral_mid or b36(self.msg_idx_pos)

    @classmethod
    def encoded_hdr(self, msg, hdr, value=None):
        hdr_value = value or (msg and msg.get(hdr)) or ''
        try:
            hdr_value.encode('us-ascii')
        except (UnicodeEncodeError, UnicodeDecodeError):
            if hdr.lower() in ('from', 'to', 'cc', 'bcc'):
                addrs = []
                for addr in [a.strip() for a in hdr_value.split(',')]:
                    name, part = [], []
                    words = addr.split()
                    for w in words:
                        if w[0] == '<' or '@' in w:
                            part.append((w, 'us-ascii'))
                        else:
                            name.append(w)
                    if name:
                        name = ' '.join(name)
                        try:
                            part[0:0] = [(name.encode('us-ascii'), 'us-ascii')]
                        except:
                            part[0:0] = [(name, 'utf-8')]
                        addrs.append(email.header.make_header(part).encode())
                hdr_value = ', '.join(addrs)
            else:
                parts = [(hdr_value, 'utf-8')]
                hdr_value = email.header.make_header(parts).encode()
        return hdr_value

    @classmethod
    def Create(cls, idx, mbox_id, mbx,
               msg_to=None, msg_cc=None, msg_bcc=None, msg_from=None,
               msg_subject=None, msg_text='', msg_references=None,
               msg_id=None, msg_atts=None,
               save=True, ephemeral_mid='not-saved', append_sig=True):
        msg = MIMEMultipart()
        msg.signature_info = msi = SignatureInfo(bubbly=False)
        msg.encryption_info = mei = EncryptionInfo(bubbly=False)
        msg_ts = int(time.time())

        if msg_from:
            from_email = AddressHeaderParser(unicode_data=msg_from)[0].address
            from_profile = idx.config.get_profile(email=from_email)
        else:
            from_profile = idx.config.get_profile()
            from_email = from_profile.get('email', None)
            from_name = from_profile.get('name', None)
            if from_email and from_name:
                msg_from = '%s <%s>' % (from_name, from_email)
        if not msg_from:
            raise NoFromAddressError()

        msg['From'] = cls.encoded_hdr(None, 'from', value=msg_from)
        msg['Date'] = email.utils.formatdate(msg_ts)
        msg['Message-Id'] = msg_id or email.utils.make_msgid('mailpile')
        msg_subj = (msg_subject or '')
        msg['Subject'] = cls.encoded_hdr(None, 'subject', value=msg_subj)

        ahp = AddressHeaderParser()
        norm = lambda a: ', '.join(sorted(list(set(ahp.normalized_addresses(
            addresses=a, with_keys=True, force_name=True)))))
        if msg_to:
            msg['To'] = cls.encoded_hdr(None, 'to', value=norm(msg_to))
        if msg_cc:
            msg['Cc'] = cls.encoded_hdr(None, 'cc', value=norm(msg_cc))
        if msg_bcc:
            msg['Bcc'] = cls.encoded_hdr(None, 'bcc', value=norm(msg_bcc))
        if msg_references:
            msg['In-Reply-To'] = msg_references[-1]
            msg['References'] = ', '.join(msg_references)

        sig = from_profile.get('signature')
        if sig and ('\n-- \n' not in (msg_text or '')):
            msg_text = (msg_text or '\n\n') + ('\n\n-- \n%s' % sig)

        if msg_text:
            try:
                msg_text.encode('us-ascii')
                charset = 'us-ascii'
            except (UnicodeEncodeError, UnicodeDecodeError):
                charset = 'utf-8'
            tp = MIMEText(msg_text, _subtype='plain', _charset=charset)
            tp.signature_info = SignatureInfo(parent=msi)
            tp.encryption_info = EncryptionInfo(parent=mei)
            msg.attach(tp)
            del tp['MIME-Version']

        if msg_atts:
            for att in msg_atts:
                att = copy.deepcopy(att)
                att.signature_info = SignatureInfo(parent=msi)
                att.encryption_info = EncryptionInfo(parent=mei)
                msg.attach(att)
                del att['MIME-Version']

        # Determine if we want to attach a PGP public key due to timing:
        if idx.config.prefs.gpg_email_key:
            addrs = ExtractEmails(norm(msg_to) + norm(msg_cc))
            offset = timedelta(days=30)
            dates = []
            for addr in addrs:
                vcard = idx.config.vcards.get(addr)
                if vcard != None:
                    lastdate = vcard.gpgshared
                    if lastdate:
                        try:
                            dates.append(datetime.fromtimestamp(float(lastdate)))
                        except ValueError:
                            pass
            if all([date+offset < datetime.now() for date in dates]):
                msg["Attach-PGP-Pubkey"] = "Yes"

        if save:
            msg_key = mbx.add(MessageAsString(msg))
            msg_to = msg_cc = []
            msg_ptr = mbx.get_msg_ptr(mbox_id, msg_key)
            msg_id = idx.get_msg_id(msg, msg_ptr)
            msg_idx, msg_info = idx.add_new_msg(msg_ptr, msg_id, msg_ts,
                                                msg_from, msg_to, msg_cc, 0,
                                                msg_subj, '', [])
            idx.set_conversation_ids(msg_info[idx.MSG_MID], msg,
                                     subject_threading=False)
            return cls(idx, msg_idx)
        else:
            msg_info = idx.edit_msg_info(idx.BOGUS_METADATA[:],
                                         msg_mid=ephemeral_mid or '',
                                         msg_id=msg['Message-ID'],
                                         msg_ts=msg_ts,
                                         msg_subject=msg_subj,
                                         msg_from=msg_from,
                                         msg_to=msg_to,
                                         msg_cc=msg_cc)
            return cls(idx, -1,
                       msg_info=msg_info,
                       msg_parsed=msg, msg_parsed_pgpmime=msg,
                       ephemeral_mid=ephemeral_mid)

    def is_editable(self, quick=False):
        if self.ephemeral_mid:
            return True
        if not self.config.is_editable_message(self.get_msg_info()):
            return False
        if quick:
            return True
        return ('x-mp-internal-readonly' not in self.get_msg())

    MIME_HEADERS = ('mime-version', 'content-type', 'content-disposition',
                    'content-transfer-encoding')
    UNEDITABLE_HEADERS = ('message-id', ) + MIME_HEADERS
    MANDATORY_HEADERS = ('From', 'To', 'Cc', 'Bcc', 'Subject',
                         'Encryption', 'Attach-PGP-Pubkey')
    HEADER_ORDER = {
        'in-reply-to': -2,
        'references': -1,
        'date': 1,
        'from': 2,
        'subject': 3,
        'to': 4,
        'cc': 5,
        'bcc': 6,
        'encryption': 98,
        'attach-pgp-pubkey': 99,
    }

    def _attachment_aid(self, att):
        aid = att.get('aid')
        if not aid:
            cid = att.get('content-id')  # This comes from afar and might
                                         # be malicious, so check it.
            if (cid and
                    cid == CleanText(cid, banned=(CleanText.WHITESPACE +
                                                  CleanText.FS)).clean):
                aid = cid
            else:
                aid = 'part:%s' % att['count']
        return aid

    def get_editing_strings(self, tree=None):
        tree = tree or self.get_message_tree()
        strings = {
            'from': '', 'to': '', 'cc': '', 'bcc': '', 'subject': '',
            'encryption': '', 'attach-pgp-pubkey': '', 'attachments': {}
        }
        header_lines = []
        body_lines = []

        # We care about header order and such things...
        hdrs = dict([(h.lower(), h) for h in tree['headers'].keys()
                     if h.lower() not in self.UNEDITABLE_HEADERS])
        for mandate in self.MANDATORY_HEADERS:
            hdrs[mandate.lower()] = hdrs.get(mandate.lower(), mandate)
        keys = hdrs.keys()
        keys.sort(key=lambda k: (self.HEADER_ORDER.get(k.lower(), 99), k))
        lowman = [m.lower() for m in self.MANDATORY_HEADERS]
        for hdr in [hdrs[k] for k in keys]:
            data = tree['headers'].get(hdr, '')
            if hdr.lower() in lowman:
                strings[hdr.lower()] = unicode(data)
            else:
                header_lines.append(unicode('%s: %s' % (hdr, data)))

        for att in tree['attachments']:
            aid = self._attachment_aid(att)
            strings['attachments'][aid] = (att['filename'] or '(unnamed)')

        if not strings['encryption']:
            strings['encryption'] = unicode(self.config.prefs.crypto_policy)

        def _fixup(t):
            try:
                return unicode(t)
            except UnicodeDecodeError:
                return t.decode('utf-8')

        strings['headers'] = '\n'.join(header_lines).replace('\r\n', '\n')
        strings['body'] = unicode(''.join([_fixup(t['data'])
                                           for t in tree['text_parts']])
                                  ).replace('\r\n', '\n')
        return strings

    def get_editing_string(self, tree=None,
                                 estrings=None,
                                 attachment_headers=True):
        if estrings is None:
            estrings = self.get_editing_strings(tree=tree)

        bits = [estrings['headers']] if estrings['headers'] else []
        for mh in self.MANDATORY_HEADERS:
            bits.append('%s: %s' % (mh, estrings[mh.lower()]))

        if attachment_headers:
            for aid in sorted(estrings['attachments'].keys()):
                bits.append('Attachment-%s: %s'
                            % (aid, estrings['attachments'][aid]))
        bits.append('')
        bits.append(estrings['body'])
        return '\n'.join(bits)

    def _update_att_name(self, part, filename):
        try:
            del part['Content-Disposition']
        except KeyError:
            pass
        part.add_header('Content-Disposition', 'attachment',
                        filename=filename)
        return part

    def _make_attachment(self, fn, msg, filedata=None):
        if filedata and fn in filedata:
            data = filedata[fn]
        else:
            if isinstance(fn, unicode):
                fn = fn.encode('utf-8')
            data = open(fn, 'rb').read()
        ctype, encoding = mimetypes.guess_type(fn)
        maintype, subtype = (ctype or 'application/octet-stream').split('/', 1)
        if maintype == 'image':
            att = MIMEImage(data, _subtype=subtype)
        else:
            att = MIMEBase(maintype, subtype)
            att.set_payload(data)
            encoders.encode_base64(att)
        att.add_header('Content-Id', MakeContentID())

        # FS paths are strings of bytes, should be represented as utf-8 for
        # correct header encoding.
        base_fn = os.path.basename(fn)
        if not isinstance(base_fn, unicode):
            base_fn = base_fn.decode('utf-8')

        att.add_header('Content-Disposition', 'attachment',
                       filename=self.encoded_hdr(None, 'file', base_fn))

        att.signature_info = SignatureInfo(parent=msg.signature_info)
        att.encryption_info = EncryptionInfo(parent=msg.encryption_info)
        return att

    def update_from_string(self, session, data, final=False):
        if not self.is_editable():
            raise NotEditableError(_('Message or mailbox is read-only.'))

        oldmsg = self.get_msg()
        if not data:
            outmsg = oldmsg

        else:
            newmsg = email.parser.Parser().parsestr(data.encode('utf-8'))
            outmsg = MIMEMultipart()
            outmsg.signature_info = SignatureInfo(bubbly=False)
            outmsg.encryption_info = EncryptionInfo(bubbly=False)

            # Copy over editable headers from the input string, skipping blanks
            for hdr in newmsg.keys():
                if hdr.startswith('Attachment-') or hdr == 'Attachment':
                    pass
                else:
                    encoded_hdr = self.encoded_hdr(newmsg, hdr)
                    if len(encoded_hdr.strip()) > 0:
                        if encoded_hdr == '!KEEP':
                            if hdr in oldmsg:
                                outmsg[hdr] = oldmsg[hdr]
                        else:
                            outmsg[hdr] = encoded_hdr

            # Copy over the uneditable headers from the old message
            for hdr in oldmsg.keys():
                if ((hdr.lower() not in self.MIME_HEADERS)
                        and (hdr.lower() in self.UNEDITABLE_HEADERS)):
                    outmsg[hdr] = oldmsg[hdr]

            # Copy the message text
            new_body = newmsg.get_payload().decode('utf-8')
            if final:
                new_body = split_long_lines(new_body)
            try:
                new_body.encode('us-ascii')
                charset = 'us-ascii'
            except (UnicodeEncodeError, UnicodeDecodeError):
                charset = 'utf-8'

            tp = MIMEText(new_body, _subtype='plain', _charset=charset)
            tp.signature_info = SignatureInfo(parent=outmsg.signature_info)
            tp.encryption_info = EncryptionInfo(parent=outmsg.encryption_info)
            outmsg.attach(tp)
            del tp['MIME-Version']

            # FIXME: Use markdown and template to generate fancy HTML part?

            # Copy the attachments we are keeping
            attachments = [h for h in newmsg.keys()
                           if h.lower().startswith('attachment')]
            if attachments:
                oldtree = self.get_message_tree()
                for att in oldtree['attachments']:
                    hdr = 'Attachment-%s' % self._attachment_aid(att)
                    if hdr in attachments:
                        outmsg.attach(self._update_att_name(att['part'],
                                                            newmsg[hdr]))
                        attachments.remove(hdr)

            # Attach some new files?
            for hdr in attachments:
                try:
                    att = self._make_attachment(newmsg[hdr], outmsg)
                    outmsg.attach(att)
                    del att['MIME-Version']
                except:
                    pass  # FIXME: Warn user that failed...

        # Save result back to mailbox
        if final:
            sender, rcpts, outmsg, ev = PrepareMessage(self.config, outmsg)
        return self.update_from_msg(session, outmsg)

    def update_from_msg(self, session, newmsg):
        if not self.is_editable():
            raise NotEditableError(_('Message or mailbox is read-only.'))

        mbx, ptr, fd = self.get_mbox_ptr_and_fd()
        fd.close()  # Windows needs this

        # OK, adding to the mailbox worked
        newptr = ptr[:MBX_ID_LEN] + mbx.add(MessageAsString(newmsg))
        self.update_parse_cache(newmsg)

        # Remove the old message...
        mbx.remove(ptr[MBX_ID_LEN:])

        # FIXME: We should DELETE the old version from the index first.

        # Update the in-memory-index
        mi = self.get_msg_info()
        mi[self.index.MSG_PTRS] = newptr
        self.index.set_msg_at_idx_pos(self.msg_idx_pos, mi)
        self.index.index_email(session, Email(self.index, self.msg_idx_pos))

        self.reset_caches(clear_parse_cache=False)
        return self

    def reset_caches(self,
                     msg_info=None, msg_parsed=None, msg_parsed_pgpmime=None,
                     clear_parse_cache=True):
        self.msg_info = msg_info
        self.msg_parsed = msg_parsed
        self.msg_parsed_pgpmime = msg_parsed_pgpmime
        if clear_parse_cache:
            self.clear_from_parse_cache()

    def update_parse_cache(self, newmsg):
        if self.msg_idx_pos >= 0 and not self.ephemeral_mid:
            with GLOBAL_PARSE_CACHE_LOCK:
                GPC = GLOBAL_PARSE_CACHE
                for i in range(0, len(GPC)):
                    if GPC[i][0] == self.msg_idx_pos:
                        GPC[i] = (self.msg_idx_pos, False, newmsg)

    def clear_from_parse_cache(self):
        if self.msg_idx_pos >= 0 and not self.ephemeral_mid:
            ClearParseCache(cache_id=self.msg_idx_pos)

    def get_msg_info(self, field=None, uncached=False):
        if (uncached or not self.msg_info) and not self.ephemeral_mid:
            self.msg_info = self.index.get_msg_at_idx_pos(self.msg_idx_pos)
        if field is None:
            return self.msg_info
        else:
            return self.msg_info[field]

    def get_mbox_ptr_and_fd(self):
        for msg_ptr in self.get_msg_info(self.index.MSG_PTRS).split(','):
            if msg_ptr == '':
                continue
            try:
                mbox = self.config.open_mailbox(None, msg_ptr[:MBX_ID_LEN])
                fd = mbox.get_file_by_ptr(msg_ptr)
                # FIXME: How do we know we have the right message?
                return mbox, msg_ptr, FixupForWith(fd)
            except (IOError, OSError, KeyError, ValueError, IndexError):
                # FIXME: If this pointer is wrong, should we fix the index?
                print 'WARNING: %s not found' % msg_ptr
        return None, None, None

    def get_file(self):
        return self.get_mbox_ptr_and_fd()[2]

    def get_msg_size(self):
        mbox, ptr, fd = self.get_mbox_ptr_and_fd()
        with fd:
            fd.seek(0, 2)
            return fd.tell()

    def _get_parsed_msg(self, pgpmime, update_cache=False):
        cache_id = self.msg_idx_pos if (self.msg_idx_pos >= 0 and
                                        not self.ephemeral_mid) else None
        return ParseMessage(self.get_file, cache_id=cache_id,
                                           update_cache=update_cache,
                                           pgpmime=pgpmime,
                                           config=self.config)

    def _update_crypto_state(self):
        if not (self.config.tags and
                self.msg_idx_pos >= 0 and
                self.msg_parsed_pgpmime and
                not self.ephemeral_mid):
            return

        import mailpile.plugins.cryptostate as cs
        kw = cs.meta_kw_extractor(self.index,
                                  self.msg_mid(),
                                  self.msg_parsed_pgpmime,
                                  0, 0)  # msg_size, msg_ts

        # We do NOT want to update tags if we are getting back
        # a none/none state, as that can happen for the more
        # complex nested crypto-in-text messages, which a more
        # forceful parse of the message may have caught earlier.
        no_sig = self.config.get_tag('mp_sig-none')
        no_sig = no_sig and '%s:in' % no_sig._key
        no_enc = self.config.get_tag('mp_enc-none')
        no_enc = no_enc and '%s:in' % no_enc._key
        if no_sig not in kw or no_enc not in kw:
            msg_info = self.get_msg_info()
            msg_tags = msg_info[self.index.MSG_TAGS].split(',')
            msg_tags = sorted([t for t in msg_tags if t])

            # Note: this has the side effect of cleaning junk off
            #       the tag list, not just updating crypto state.
            def tcheck(tag_id):
                tag = self.config.get_tag(tag_id)
                return (tag and tag.slug[:6] not in ('mp_enc', 'mp_sig'))
            new_tags = sorted([t for t in msg_tags if tcheck(t)] +
                              [ti.split(':', 1)[0] for ti in kw
                               if ti.endswith(':in')])

            if msg_tags != new_tags:
                msg_info[self.index.MSG_TAGS] = ','.join(new_tags)
                self.index.set_msg_at_idx_pos(self.msg_idx_pos, msg_info)

    def get_msg(self, pgpmime=True, crypto_state_feedback=True):
        if pgpmime:
            if self.msg_parsed_pgpmime:
                result = self.msg_parsed_pgpmime
            else:
                result = self._get_parsed_msg(pgpmime)
                self.msg_parsed_pgpmime = result

                # Post-parse, we want to make sure that the crypto-state
                # recorded on this message's metadata is up to date.
                if crypto_state_feedback:
                    self._update_crypto_state()
        else:
            if not self.msg_parsed:
                self.msg_parsed = self._get_parsed_msg(pgpmime)
            result = self.msg_parsed
        if not result:
            raise IndexError(_('Message not found?'))
        return result

    def get_headerprint(self):
        return HeaderPrint(self.get_msg())

    def is_thread(self):
        return ((self.get_msg_info(self.index.MSG_THREAD_MID)) or
                (0 < len(self.get_msg_info(self.index.MSG_REPLIES))))

    def get(self, field, default=''):
        """Get one (or all) indexed fields for this mail."""
        field = field.lower()
        if field == 'subject':
            return self.get_msg_info(self.index.MSG_SUBJECT)
        elif field == 'from':
            return self.get_msg_info(self.index.MSG_FROM)
        else:
            raw = ' '.join(self.get_msg().get_all(field, default))
            return self.index.hdr(0, 0, value=raw) or raw

    def get_msg_summary(self):
        # We do this first to make sure self.msg_info is loaded
        msg_mid = self.get_msg_info(self.index.MSG_MID)
        return [
            msg_mid,
            self.get_msg_info(self.index.MSG_ID),
            self.get_msg_info(self.index.MSG_FROM),
            self.index.expand_to_list(self.msg_info),
            self.get_msg_info(self.index.MSG_SUBJECT),
            self.get_msg_info(self.index.MSG_BODY),
            self.get_msg_info(self.index.MSG_DATE),
            self.get_msg_info(self.index.MSG_TAGS).split(','),
            self.is_editable(quick=True)
        ]

    def _find_attachments(self, att_id, negative=False):
        msg = self.get_msg()
        count = 0
        for part in (msg.walk() if msg else []):
            mimetype = part.get_content_type()
            if mimetype.startswith('multipart/'):
                continue

            count += 1
            content_id = part.get('content-id', '')
            pfn = self.index.hdr(0, 0, value=part.get_filename() or '')

            if (('*' == att_id)
                    or ('#%s' % count == att_id)
                    or ('part:%s' % count == att_id)
                    or (content_id == att_id)
                    or (mimetype == att_id)
                    or (pfn.lower().endswith('.%s' % att_id))
                    or (pfn == att_id)):
                if not negative:
                    yield (count, content_id, pfn, mimetype, part)
            elif negative:
                yield (count, content_id, pfn, mimetype, part)

    def add_attachments(self, session, filenames, filedata=None):
        if not self.is_editable():
            raise NotEditableError(_('Message or mailbox is read-only.'))
        msg = self.get_msg()
        for fn in filenames:
            att = self._make_attachment(fn, msg, filedata=filedata)
            msg.attach(att)
            del att['MIME-Version']
        return self.update_from_msg(session, msg)

    def remove_attachments(self, session, *att_ids):
        if not self.is_editable():
            raise NotEditableError(_('Message or mailbox is read-only.'))

        remove = []
        for att_id in att_ids:
            for count, cid, pfn, mt, part in self._find_attachments(att_id):
                remove.append(self._attachment_aid({
                    'msg_mid': self.msg_mid(),
                    'count': count,
                    'content-id': cid,
                    'filename': pfn,
                }))

        es = self.get_editing_strings()
        es['headers'] = None
        for k in remove:
            if k in es['attachments']:
                del es['attachments'][k]

        estring = self.get_editing_string(estrings=es)
        return self.update_from_string(session, estring)

    def extract_attachment(self, session, att_id,
                           name_fmt=None, mode='download'):
        extracted = 0
        filename, attributes = '', {}
        for (count, content_id, pfn, mimetype, part
                ) in self._find_attachments(att_id):
            payload = part.get_payload(None, True) or ''
            attributes = {
                'msg_mid': self.msg_mid(),
                'count': count,
                'length': len(payload),
                'content-id': content_id,
                'filename': pfn,
            }
            attributes['aid'] = self._attachment_aid(attributes)
            if pfn:
                if '.' in pfn:
                    pfn, attributes['att_ext'] = pfn.rsplit('.', 1)
                    attributes['att_ext'] = attributes['att_ext'].lower()
                attributes['att_name'] = pfn
            if mimetype:
                attributes['mimetype'] = mimetype

            filesize = len(payload)
            if mode.startswith('inline'):
                attributes['data'] = payload
                session.ui.notify(_('Extracted attachment %s') % att_id)
            elif mode.startswith('preview'):
                attributes['thumb'] = True
                attributes['mimetype'] = 'image/jpeg'
                attributes['disposition'] = 'inline'
                thumb = StringIO.StringIO()
                if thumbnail(payload, thumb, height=250):
                    attributes['length'] = thumb.tell()
                    filename, fd = session.ui.open_for_data(
                        name_fmt=name_fmt, attributes=attributes)
                    thumb.seek(0)
                    fd.write(thumb.read())
                    fd.close()
                    session.ui.notify(_('Wrote preview to: %s') % filename)
                else:
                    session.ui.notify(_('Failed to generate thumbnail'))
                    raise UrlRedirectException('/static/img/image-default.png')
            else:
                filename, fd = session.ui.open_for_data(
                    name_fmt=name_fmt, attributes=attributes)
                fd.write(payload)
                session.ui.notify(_('Wrote attachment to: %s') % filename)
                fd.close()
            extracted += 1

        if 0 == extracted:
            session.ui.notify(_('No attachments found for: %s') % att_id)
            return None, None
        else:
            return filename, attributes

    def get_message_tags(self):
        tids = self.get_msg_info(self.index.MSG_TAGS).split(',')
        return [self.config.get_tag(t) for t in tids]

    RE_HTML_BORING = re.compile('(\s+|<style[^>]*>[^<>]*</style>)')
    RE_EXCESS_WHITESPACE = re.compile('\n\s*\n\s*')
    RE_HTML_NEWLINES = re.compile('(<br|</(tr|table))')
    RE_HTML_PARAGRAPHS = re.compile('(</?p|</?(title|div|html|body))')
    RE_HTML_LINKS = re.compile('<a\s+[^>]*href=[\'"]?([^\'">]+)[^>]*>'
                               '([^<]*)</a>')
    RE_HTML_IMGS = re.compile('<img\s+[^>]*src=[\'"]?([^\'">]+)[^>]*>')
    RE_HTML_IMG_ALT = re.compile('<img\s+[^>]*alt=[\'"]?([^\'">]+)[^>]*>')

    def _extract_text_from_html(self, html):
        try:
            # We compensate for some of the limitations of lxml...
            links, imgs = [], []
            def delink(m):
                url, txt = m.group(1), m.group(2).strip()
                if txt[:4] in ('http', 'www.'):
                    return txt
                elif url.startswith('mailto:'):
                    if '@' in txt:
                        return txt
                    else:
                        return '%s (%s)' % (txt, url.split(':', 1)[1])
                else:
                    links.append(' [%d] %s%s' % (len(links) + 1,
                                                 txt and (txt + ': ') or '',
                                                 url))
                    return '%s[%d]' % (txt, len(links))
            def deimg(m):
                tag, url = m.group(0), m.group(1)
                if ' alt=' in tag:
                    return re.sub(self.RE_HTML_IMG_ALT, '\1', tag).strip()
                else:
                    imgs.append(' [%d] %s' % (len(imgs)+1, url))
                    return '[Image %d]' % len(imgs)
            html = re.sub(self.RE_HTML_PARAGRAPHS, '\n\n\\1',
                       re.sub(self.RE_HTML_NEWLINES, '\n\\1',
                           re.sub(self.RE_HTML_BORING, ' ',
                               re.sub(self.RE_HTML_LINKS, delink,
                                   re.sub(self.RE_HTML_IMGS, deimg, html)))))
            if html.strip() != '':
                try:
                    html_text = lxml.html.fromstring(html).text_content()
                except XMLSyntaxError:
                    html_text = _('(Invalid HTML suppressed)')
            else:
                html_text = ''
            text = (html_text +
                    (links and '\n\nLinks:\n' or '') + '\n'.join(links) +
                    (imgs and '\n\nImages:\n' or '') + '\n'.join(imgs))
            return re.sub(self.RE_EXCESS_WHITESPACE, '\n\n', text).strip()
        except:
            import traceback
            traceback.print_exc()
            return html

    def get_message_tree(self, want=None):
        msg = self.get_msg()
        want = list(want) if (want is not None) else None
        tree = {
            'id': self.get_msg_info(self.index.MSG_ID)
        }

        if want is not None:
            if 'editing_strings' in want or 'editing_string' in want:
                want.extend(['text_parts', 'headers', 'attachments'])

        for p in 'text_parts', 'html_parts', 'attachments':
            if want is None or p in want:
                tree[p] = []

        if want is None or 'summary' in want:
            tree['summary'] = self.get_msg_summary()

        if want is None or 'tags' in want:
            tree['tags'] = self.get_msg_info(self.index.MSG_TAGS).split(',')

        if want is None or 'conversation' in want:
            tree['conversation'] = {}
            conv_id = self.get_msg_info(self.index.MSG_THREAD_MID)
            if conv_id:
                conv = Email(self.index, int(conv_id, 36))
                tree['conversation'] = convs = [conv.get_msg_summary()]
                for rid in conv.get_msg_info(self.index.MSG_REPLIES
                                             ).split(','):
                    if rid:
                        convs.append(Email(self.index, int(rid, 36)
                                           ).get_msg_summary())

        if (want is None or 'headers' in want):
            tree['headers'] = {}
            for hdr in msg.keys():
                tree['headers'][hdr] = self.index.hdr(msg, hdr)

        if want is None or 'headers_lc' in want:
            tree['headers_lc'] = {}
            for hdr in msg.keys():
                tree['headers_lc'][hdr.lower()] = self.index.hdr(msg, hdr)

        if want is None or 'header_list' in want:
            tree['header_list'] = [(k, self.index.hdr(msg, k, value=v))
                                   for k, v in msg.items()]

        if want is None or 'addresses' in want:
            tree['addresses'] = {}
            for hdr in msg.keys():
                hdrl = hdr.lower()
                if hdrl in ('reply-to', 'from', 'to', 'cc', 'bcc'):
                    tree['addresses'][hdrl] = AddressHeaderParser(msg[hdr])

        # FIXME: Decide if this is strict enough or too strict...?
        html_cleaner = Cleaner(page_structure=True, meta=True, links=True,
                               javascript=True, scripts=True, frames=True,
                               embedded=True, safe_attrs_only=True)

        # Note: count algorithm must match that used in extract_attachment
        #       above
        count = 0
        for part in msg.walk():
            crypto = {
                'signature': part.signature_info,
                'encryption': part.encryption_info,
            }

            mimetype = part.get_content_type()
            if (mimetype.startswith('multipart/')
                    or mimetype == "application/pgp-encrypted"):
                continue
            try:
                if (mimetype == "application/octet-stream"
                        and part.cryptedcontainer is True):
                    continue
            except:
                pass

            count += 1
            if (part.get('content-disposition', 'inline') == 'inline'
                    and mimetype in ('text/plain', 'text/html')):
                payload, charset = self.decode_payload(part)
                start = payload[:100].strip()

                if mimetype == 'text/html':
                    if want is None or 'html_parts' in want:
                        tree['html_parts'].append({
                            'charset': charset,
                            'type': 'html',
                            'data': ((payload.strip()
                                      and html_cleaner.clean_html(payload))
                                     or '')
                        })

                elif want is None or 'text_parts' in want:
                    if start[:3] in ('<di', '<ht', '<p>', '<p ', '<ta', '<bo'):
                        payload = self._extract_text_from_html(payload)
                    # Ignore white-space only text parts, they usually mean
                    # the message is HTML only and we want the code below
                    # to try and extract meaning from it.
                    if (start or payload.strip()) != '':
                        text_parts = self.parse_text_part(payload, charset,
                                                          crypto)
                        tree['text_parts'].extend(text_parts)

            elif want is None or 'attachments' in want:
                att = {
                    'mimetype': mimetype,
                    'count': count,
                    'part': part,
                    'length': len(part.get_payload(None, True) or ''),
                    'content-id': part.get('content-id', ''),
                    'filename': self.index.hdr(0, 0,
                                               value=part.get_filename() or ''),
                    'crypto': crypto
                }
                att['aid'] = self._attachment_aid(att)
                tree['attachments'].append(att)

        if want is None or 'text_parts' in want:
            if tree.get('html_parts') and not tree.get('text_parts'):
                html_part = tree['html_parts'][0]
                payload = self._extract_text_from_html(html_part['data'])
                text_parts = self.parse_text_part(payload,
                                                  html_part['charset'],
                                                  crypto)
                tree['text_parts'].extend(text_parts)

        if self.is_editable():
            if not want or 'editing_strings' in want:
                tree['editing_strings'] = self.get_editing_strings(tree)
            if not want or 'editing_string' in want:
                tree['editing_string'] = self.get_editing_string(tree)

        if want is None or 'crypto' in want:
            if 'crypto' not in tree:
                tree['crypto'] = {'encryption': msg.encryption_info,
                                  'signature': msg.signature_info}
            else:
                tree['crypto']['encryption'] = msg.encryption_info
                tree['crypto']['signature'] = msg.signature_info

        msg.signature_info.mix_bubbles()
        msg.encryption_info.mix_bubbles()
        return tree

    # FIXME: This should be configurable by the user, depending on where
    #        he lives and what kind of e-mail he gets.
    CHARSET_PRIORITY_LIST = ['utf-8', 'iso-8859-1']

    def decode_text(self, payload, charset='utf-8', binary=True):
        if charset:
            charsets = [charset] + [c for c in self.CHARSET_PRIORITY_LIST
                                    if charset.lower() != c]
        else:
            charsets = self.CHARSET_PRIORITY_LIST

        for charset in charsets:
            try:
                payload = payload.decode(charset)
                return payload, charset
            except (UnicodeDecodeError, TypeError, LookupError):
                pass

        if binary:
            return payload, '8bit'
        else:
            return _('[Binary data suppressed]\n'), 'utf-8'

    def decode_payload(self, part):
        charset = part.get_content_charset() or None
        payload = part.get_payload(None, True) or ''
        return self.decode_text(payload, charset=charset)

    def parse_text_part(self, data, charset, crypto):
        psi = crypto['signature']
        pei = crypto['encryption']
        current = {
            'type': 'bogus',
            'charset': charset,
            'crypto': {
                'signature': SignatureInfo(parent=psi),
                'encryption': EncryptionInfo(parent=pei)
            }
        }
        parse = []
        block = 'body'
        clines = []
        for line in data.splitlines(True):
            block, ltype = self.parse_line_type(line, block)
            if ltype != current['type']:

                # This is not great, it's a hack to move the preamble
                # before a quote section into the quote itself.
                if ltype == 'quote' and clines and '@' in clines[-1]:
                    current['data'] = ''.join(clines[:-1])
                    clines = clines[-1:]
                elif (ltype == 'quote' and len(clines) > 2
                        and '@' in clines[-2] and '' == clines[-1].strip()):
                    current['data'] = ''.join(clines[:-2])
                    clines = clines[-2:]
                else:
                    clines = []

                current = {
                    'type': ltype,
                    'data': ''.join(clines),
                    'charset': charset,
                    'crypto': {
                        'signature': SignatureInfo(parent=psi),
                        'encryption': EncryptionInfo(parent=pei)
                    }
                }
                parse.append(current)
            current['data'] += line
            clines.append(line)
        return parse

    def parse_line_type(self, line, block):
        # FIXME: Detect forwarded messages, ...

        if block in ('body', 'quote') and line in ('-- \n', '-- \r\n',
                                                   '- --\n', '- --\r\n'):
            return 'signature', 'signature'

        if block == 'signature':
            return 'signature', 'signature'

        stripped = line.rstrip()

        if stripped == GnuPG.ARMOR_BEGIN_SIGNED:
            return 'pgpbeginsigned', 'pgpbeginsigned'
        if block == 'pgpbeginsigned':
            if line.startswith('Hash: ') or stripped == '':
                return 'pgpbeginsigned', 'pgpbeginsigned'
            else:
                return 'pgpsignedtext', 'pgpsignedtext'
        if block == 'pgpsignedtext':
            if stripped == GnuPG.ARMOR_BEGIN_SIGNATURE:
                return 'pgpsignature', 'pgpsignature'
            else:
                return 'pgpsignedtext', 'pgpsignedtext'
        if block == 'pgpsignature':
            if stripped == GnuPG.ARMOR_END_SIGNATURE:
                return 'pgpend', 'pgpsignature'
            else:
                return 'pgpsignature', 'pgpsignature'

        if stripped == GnuPG.ARMOR_BEGIN_ENCRYPTED:
            return 'pgpbegin', 'pgpbegin'
        if block == 'pgpbegin':
            if ':' in line or stripped == '':
                return 'pgpbegin', 'pgpbegin'
            else:
                return 'pgptext', 'pgptext'
        if block == 'pgptext':
            if stripped == GnuPG.ARMOR_END_ENCRYPTED:
                return 'pgpend', 'pgpend'
            else:
                return 'pgptext', 'pgptext'

        if block == 'quote':
            if stripped == '':
                return 'quote', 'quote'
        if line.startswith('>'):
            return 'quote', 'quote'

        return 'body', 'text'

    WANT_MSG_TREE_PGP = ('text_parts', 'crypto')
    PGP_OK = {
        'pgpbeginsigned': 'pgpbeginverified',
        'pgpsignedtext': 'pgpverifiedtext',
        'pgpsignature': 'pgpverification',
        'pgpbegin': 'pgpbeginverified',
        'pgptext': 'pgpsecuretext',
        'pgpend': 'pgpverification',
    }

    def evaluate_pgp(self, tree, check_sigs=True, decrypt=False,
                                 crypto_state_feedback=True):
        if 'text_parts' not in tree:
            return tree

        pgpdata = []
        for part in tree['text_parts']:
            if 'crypto' not in part:
                part['crypto'] = {}

            ei = si = None
            if check_sigs:
                if part['type'] == 'pgpbeginsigned':
                    pgpdata = [part]
                elif part['type'] == 'pgpsignedtext':
                    pgpdata.append(part)
                elif part['type'] == 'pgpsignature':
                    pgpdata.append(part)
                    try:
                        gpg = GnuPG(self.config)
                        message = ''.join([p['data'].encode(p['charset'])
                                           for p in pgpdata])
                        si = gpg.verify(message)
                        pgpdata[0]['data'] = ''
                        pgpdata[1]['crypto']['signature'] = si
                        pgpdata[2]['data'] = ''

                    except Exception, e:
                        print e

            if decrypt:
                if part['type'] in ('pgpbegin', 'pgptext'):
                    pgpdata.append(part)
                elif part['type'] == 'pgpend':
                    pgpdata.append(part)

                    data = ''.join([p['data'] for p in pgpdata])
                    gpg = GnuPG(self.config)
                    si, ei, text = gpg.decrypt(data)

                    # FIXME: If the data is binary, we should provide some
                    #        sort of download link or maybe leave the PGP
                    #        blob entirely intact, undecoded.
                    text, charset = self.decode_text(text, binary=False)

                    pgpdata[1]['crypto']['encryption'] = ei
                    pgpdata[1]['crypto']['signature'] = si
                    if ei["status"] == "decrypted":
                        pgpdata[0]['data'] = ""
                        pgpdata[1]['data'] = text
                        pgpdata[2]['data'] = ""

            # Bubbling up!
            if (si or ei) and 'crypto' not in tree:
                tree['crypto'] = {'signature': SignatureInfo(bubbly=False),
                                  'encryption': EncryptionInfo(bubbly=False)}
            if si:
                si.bubble_up(tree['crypto']['signature'])
            if ei:
                ei.bubble_up(tree['crypto']['encryption'])

        # Cleanup, remove empty 'crypto': {} blocks.
        for part in tree['text_parts']:
            if not part['crypto']:
                del part['crypto']

        tree['crypto']['signature'].mix_bubbles()
        tree['crypto']['encryption'].mix_bubbles()
        if crypto_state_feedback:
            self._update_crypto_state()
        return tree

    def _decode_gpg(self, message, decrypted):
        header, body = message.replace('\r\n', '\n').split('\n\n', 1)
        for line in header.lower().split('\n'):
            if line.startswith('charset:'):
                return decrypted.decode(line.split()[1])
        return decrypted.decode('utf-8')


class AddressHeaderParser(list):
    """
    This is a class which tries very hard to interpret the From:, To:
    and Cc: lines found in real-world e-mail and make sense of them.

    The general strategy of this parser is to:
       1. parse header data into tokens
       2. group tokens together into address + name constructs.

    And optionaly,
       3. normalize each group to a standard format

    In practice, we do this in multiple passes: first a strict pass where
    we try to parse things semi-sensibly, followed by fuzzier heuristics.

    Ideally, if folks format things correctly we should parse correctly.
    But if that fails, there are are other passes where we try to cope
    with various types of weirdness we've seen in the wild. The wild can
    be pretty wild.

    This parser is NOT (yet) fully RFC2822 compliant - in particular it
    will get confused by nested comments (see FIXME in tests below).

    Examples:

    >>> ahp = AddressHeaderParser(AddressHeaderParser.TEST_HEADER_DATA)
    >>> ai = ahp[1]
    >>> ai.fn
    u'Bjarni'
    >>> ai.address
    u'bre@klaki.net'
    >>> ahp.normalized_addresses() == ahp.TEST_EXPECT_NORMALIZED_ADDRESSES
    True

    >>> AddressHeaderParser('Weird email@somewhere.com Header').normalized()
    u'"Weird Header" <email@somewhere.com>'

    >>> ai = AddressHeaderParser(unicode_data=ahp.TEST_UNICODE_DATA)
    >>> ai[0].fn
    u'Bjarni R\\xfanar'
    >>> ai[0].fn == ahp.TEST_UNICODE_NAME
    True
    >>> ai[0].address
    u'b@c.x'
    """

    TEST_UNICODE_DATA = u'Bjarni R\xfanar <b@c.x#61A015763D28D4>'
    TEST_UNICODE_NAME = u'Bjarni R\xfanar'
    TEST_HEADER_DATA = """
        bre@klaki.net  ,
        bre@klaki.net Bjarni ,
        bre@klaki.net bre@klaki.net,
        bre@klaki.net (bre@notmail.com),
        bre@klaki.net ((nested) bre@notmail.com comment),
        (FIXME: (nested) bre@wrongmail.com parser breaker) bre@klaki.net,
        undisclosed-recipients-gets-ignored:,
        Bjarni [mailto:bre@klaki.net],
        "This is a key test" <bre@klaki.net#61A015763D28D410A87B197328191D9B3B4199B4>,
        bre@klaki.net (Bjarni Runar Einar's son);
        Bjarni is bre @klaki.net,
        Bjarni =?iso-8859-1?Q?Runar?=Einarsson<' bre'@ klaki.net>,
    """
    TEST_EXPECT_NORMALIZED_ADDRESSES = [
        '<bre@klaki.net>',
        '"Bjarni" <bre@klaki.net>',
        '"bre@klaki.net" <bre@klaki.net>',
        '"bre@notmail.com" <bre@klaki.net>',
        '"(nested bre@notmail.com comment)" <bre@klaki.net>',
        '"(FIXME: nested parser breaker) bre@klaki.net" <bre@wrongmail.com>',
        '"Bjarni" <bre@klaki.net>',
        '"This is a key test" <bre@klaki.net>',
        '"Bjarni Runar Einar\\\'s son" <bre@klaki.net>',
        '"Bjarni is" <bre@klaki.net>',
        '"Bjarni Runar Einarsson" <bre@klaki.net>']

    # Escaping and quoting
    TXT_RE_QUOTE = '=\\?([^\\?\\s]+)\\?([QqBb])\\?([^\\?\\s]+)\\?='
    TXT_RE_QUOTE_NG = TXT_RE_QUOTE.replace('(', '(?:')
    RE_ESCAPES = re.compile('\\\\([\\\\"\'])')
    RE_QUOTED = re.compile(TXT_RE_QUOTE)
    RE_SHOULD_ESCAPE = re.compile('([\\\\"\'])')
    RE_SHOULD_QUOTE = re.compile('[^a-zA-Z0-9()\.:/_ \'"+@-]')

    # This is how we normally break a header line into tokens
    RE_TOKENIZER = re.compile('(<[^<>]*>'                    # <stuff>
                              '|\\([^\\(\\)]*\\)'            # (stuff)
                              '|\\[[^\\[\\]]*\\]'            # [stuff]
                              '|"(?:\\\\\\\\|\\\\"|[^"])*"'  # "stuff"
                              "|'(?:\\\\\\\\|\\\\'|[^'])*'"  # 'stuff'
                              '|' + TXT_RE_QUOTE_NG +        # =?stuff?=
                              '|,'                           # ,
                              '|;'                           # ;
                              '|\\s+'                        # white space
                              '|[^\\s;,]+'                   # non-white space
                              ')')

    # Where to insert spaces to help the tokenizer parse bad data
    RE_MUNGE_TOKENSPACERS = (re.compile('(\S)(<)'), re.compile('(\S)(=\\?)'))

    # Characters to strip aware entirely when tokenizing munged data
    RE_MUNGE_TOKENSTRIPPERS = (re.compile('[<>"]'),)

    # This is stuff we ignore (undisclosed-recipients, etc)
    RE_IGNORED_GROUP_TOKENS = re.compile('(?i)undisclosed')

    # Things we strip out to try and un-mangle e-mail addresses when
    # working with bad data.
    RE_MUNGE_STRIP = re.compile('(?i)(?:\\bmailto:|[\\s"\']|\?$)')

    # This a simple regular expression for detecting e-mail addresses.
    RE_MAYBE_EMAIL = re.compile('^[^()<>@,;:\\\\"\\[\\]\\s\000-\031]+'
                                '@[a-zA-Z0-9_\\.-]+(?:#[A-Za-z0-9]+)?$')

    # We try and interpret non-ascii data as a particular charset, in
    # this order by default. Should be overridden whenever we have more
    # useful info from the message itself.
    DEFAULT_CHARSET_ORDER = ('iso-8859-1', 'utf-8')

    def __init__(self,
                 data=None, unicode_data=None, charset_order=None, **kwargs):
        self.charset_order = charset_order or self.DEFAULT_CHARSET_ORDER
        self._parse_args = kwargs
        if data is None and unicode_data is None:
            self._reset(**kwargs)
        elif data is not None:
            self.parse(data)
        else:
            self.charset_order = ['utf-8']
            self.parse(unicode_data.encode('utf-8'))

    def _reset(self, _raw_data=None, strict=False, _raise=False):
        self._raw_data = _raw_data
        self._tokens = []
        self._groups = []
        self[:] = []

    def parse(self, data):
        return self._parse(data, **self._parse_args)

    def _parse(self, data, strict=False, _raise=False):
        self._reset(_raw_data=data)

        # 1st pass, strict
        try:
            self._tokens = self._tokenize(self._raw_data)
            self._groups = self._group(self._tokens)
            self[:] = self._find_addresses(self._groups,
                                           _raise=(not strict))
            return self
        except ValueError:
            if strict and _raise:
                raise
        if strict:
            return self

        # 2nd & 3rd passes; various types of sloppy
        for _pass in ('2', '3'):
            try:
                self._tokens = self._tokenize(self._raw_data, munge=_pass)
                self._groups = self._group(self._tokens, munge=_pass)
                self[:] = self._find_addresses(self._groups,
                                               munge=_pass,
                                               _raise=_raise)
                return self
            except ValueError:
                if _pass == 3 and _raise:
                    raise
        return self

    def unquote(self, string, charset_order=None):
        def uq(m):
            cs, how, data = m.group(1), m.group(2), m.group(3)
            if how in ('b', 'B'):
                return base64.b64decode(data).decode(cs)
            else:
                return quopri.decodestring(data, header=True).decode(cs)

        for cs in charset_order or self.charset_order:
            try:
                string = string.decode(cs)
                break
            except UnicodeDecodeError:
                pass

        return re.sub(self.RE_QUOTED, uq, string)

    @classmethod
    def unescape(self, string):
        return re.sub(self.RE_ESCAPES, lambda m: m.group(1), string)

    @classmethod
    def escape(self, strng):
        return re.sub(self.RE_SHOULD_ESCAPE, lambda m: '\\'+m.group(0), strng)

    @classmethod
    def quote(self, strng):
        if re.search(self.RE_SHOULD_QUOTE, strng):
            enc = quopri.encodestring(strng.encode('utf-8'), False,
                                      header=True)
            return '=?utf-8?Q?%s?=' % enc
        else:
            return '"%s"' % self.escape(strng)

    def _tokenize(self, string, munge=False):
        if munge:
            for ts in self.RE_MUNGE_TOKENSPACERS:
                string = re.sub(ts, '\\1 \\2', string)
            if munge == 3:
                for ts in self.RE_MUNGE_TOKENSTRIPPERS:
                    string = re.sub(ts, '', string)
        return re.findall(self.RE_TOKENIZER, string)

    def _clean(self, token):
        if token[:1] in ('"', "'"):
            if token[:1] == token[-1:]:
                return self.unescape(token[1:-1])
        elif token.startswith('[mailto:') and token[-1:] == ']':
            # Just convert [mailto:...] crap into a <address>
            return '<%s>' % token[8:-1]
        elif (token[:1] == '[' and token[-1:] == ']'):
            return token[1:-1]
        return token

    def _group(self, tokens, munge=False):
        groups = [[]]
        for token in tokens:
            token = token.strip()
            if token in (',', ';'):
                # Those tokens SHOULD separate groups, but we don't like to
                # create groups that have no e-mail addresses at all.
                if groups[-1]:
                    if [g for g in groups[-1] if '@' in g]:
                        groups.append([])
                        continue
                    # However, this stuff is just begging to be ignored.
                    elif [g for g in groups[-1]
                          if re.match(self.RE_IGNORED_GROUP_TOKENS, g)]:
                        groups[-1] = []
                        continue
            if token:
                groups[-1].append(self.unquote(self._clean(token)))
        if not groups[-1]:
            groups.pop(-1)
        return groups

    def _find_addresses(self, groups, **fa_kwargs):
        alist = [self._find_address(g, **fa_kwargs) for g in groups]
        return [a for a in alist if a]

    def _find_address(self, g, _raise=False, munge=False):
        if g:
            g = g[:]
        else:
            return []

        def email_at(i):
            for j in range(0, len(g)):
                if g[j][:1] == '(' and g[j][-1:] == ')':
                    g[j] = g[j][1:-1]
            rest = ' '.join([g[j] for j in range(0, len(g)) if j != i
                             ]).replace(' ,', ',').replace(' ;', ';')
            email, keys = g[i], None
            if '#' in email[email.index('@'):]:
                email, key = email.rsplit('#', 1)
                keys = [{'fingerprint': key}]
            return AddressInfo(email, rest.strip(), keys=keys)

        def munger(string):
            if munge:
                return re.sub(self.RE_MUNGE_STRIP, '', string)
            else:
                return string

        # If munging, look for email @domain.com in two parts, rejoin
        if munge:
            for i in range(0, len(g)):
                if i > 0 and i < len(g) and g[i][:1] == '@':
                    g[i-1:i+1] = [g[i-1]+g[i]]
                elif i < len(g)-1 and g[i][-1:] == '@':
                    g[i:i+2] = [g[i]+g[i+1]]

        # 1st, look for <email@domain.com>
        for i in range(0, len(g)):
            if g[i][:1] == '<' and g[i][-1:] == '>':
                maybemail = munger(g[i][1:-1])
                if re.match(self.RE_MAYBE_EMAIL, maybemail):
                    g[i] = maybemail
                    return email_at(i)

        # 2nd, look for bare email@domain.com
        for i in range(0, len(g)):
            maybemail = munger(g[i])
            if re.match(self.RE_MAYBE_EMAIL, maybemail):
                g[i] = maybemail
                return email_at(i)

        if _raise:
            raise ValueError('No email found in %s' % (g,))
        else:
            return None

    def normalized_addresses(self,
                             addresses=None, quote=True, with_keys=False,
                             force_name=False):
        if addresses is None:
            addresses = self
        elif not addresses:
            addresses = []
        def fmt(ai):
            email = ai.address
            if with_keys and ai.keys:
                fp = ai.keys[0].get('fingerprint')
                epart = '<%s%s>' % (email, fp and ('#%s' % fp) or '')
            else:
                epart = '<%s>' % email
            if ai.fn:
                 return ' '.join([quote and self.quote(ai.fn) or ai.fn, epart])
            elif force_name:
                 return ' '.join([quote and self.quote(email) or email, epart])
            else:
                 return epart
        return [fmt(ai) for ai in addresses]

    def normalized(self, **kwargs):
        return ', '.join(self.normalized_addresses(**kwargs))


if __name__ == "__main__":
    import doctest
    import sys
    results = doctest.testmod(optionflags=doctest.ELLIPSIS,
                              extraglobs={})
    print '%s' % (results, )
    if results.failed:
        sys.exit(1)