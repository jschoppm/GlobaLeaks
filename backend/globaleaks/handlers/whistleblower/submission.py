# -*- coding: utf-8 -*-
#
# Handlerse dealing with submission interface
import base64
import json

from globaleaks import models
from globaleaks.handlers.admin.questionnaire import db_get_questionnaire
from globaleaks.handlers.base import BaseHandler
from globaleaks.orm import db_get, db_log, transact
from globaleaks.rest import errors, requests
from globaleaks.state import State
from globaleaks.utils.crypto import sha256, Base64Encoder, GCE
from globaleaks.utils.json import JSONEncoder
from globaleaks.utils.utility import get_expiration, datetime_null


def decrypt_tip(user_key, tip_prv_key, tip):
    tip_key = GCE.asymmetric_decrypt(user_key, tip_prv_key)

    if 'label' in tip and tip['label']:
        tip['label'] = GCE.asymmetric_decrypt(tip_key, base64.b64decode(tip['label'].encode())).decode()

    for questionnaire in tip['questionnaires']:
        questionnaire['answers'] = json.loads(GCE.asymmetric_decrypt(tip_key, base64.b64decode(questionnaire['answers'].encode())).decode())

    for k in ['whistleblower_identity']:
        if k in tip['data'] and tip['data'][k]:
            tip['data'][k] = json.loads(GCE.asymmetric_decrypt(tip_key, base64.b64decode(tip['data'][k].encode())).decode())

            if k == 'whistleblower_identity' and isinstance(tip['data'][k], list):
                # Fix for issue: https://github.com/globaleaks/GlobaLeaks/issues/2612
                # The bug is due to the fact that the data was initially saved as an array of one entry
                tip['data'][k] = tip['data'][k][0]

    if 'iar' in tip:
        if tip['iar']['request_motivation']:
            tip['iar']['request_motivation'] = GCE.asymmetric_decrypt(tip_key, base64.b64decode(tip['iar']['request_motivation'])).decode()

        if tip['iar']['reply_motivation']:
            tip['iar']['reply_motivation'] = GCE.asymmetric_decrypt(tip_key, base64.b64decode(tip['iar']['reply_motivation'])).decode()

    for x in tip['comments']:
        x['content'] = GCE.asymmetric_decrypt(tip_key, base64.b64decode(x['content'].encode())).decode()

    for x in tip['rfiles'] + tip['wbfiles']:
        for k in ['name', 'description', 'type', 'size']:
            if k in x and x[k]:
                x[k] = GCE.asymmetric_decrypt(tip_key, base64.b64decode(x[k].encode())).decode()
                if k == 'size':
                    x[k] = int(x[k])

    return tip


def db_set_internaltip_answers(session, itip_id, questionnaire_hash, answers):
    x = session.query(models.InternalTipAnswers) \
               .filter(models.InternalTipAnswers.internaltip_id == itip_id,
                       models.InternalTipAnswers.questionnaire_hash == questionnaire_hash).one_or_none()

    if x is not None:
        return

    ita = models.InternalTipAnswers()
    ita.internaltip_id = itip_id
    ita.questionnaire_hash = questionnaire_hash
    ita.answers = answers
    session.add(ita)


def db_set_internaltip_data(session, itip_id, key, value):
    x = session.query(models.InternalTipData) \
               .filter(models.InternalTipData.internaltip_id == itip_id,
                       models.InternalTipData.key == key).one_or_none()

    if x is not None:
        return

    itd = models.InternalTipData()
    itd.internaltip_id = itip_id
    itd.key = key
    itd.value = value
    session.add(itd)


def db_assign_submission_progressive(session, tid):
    counter = session.query(models.Config).filter(models.Config.tid == tid, models.Config.var_name == 'counter_submissions').one()
    counter.value += 1
    return counter.value


def db_archive_questionnaire_schema(session, questionnaire):
    hash = sha256(json.dumps(questionnaire, sort_keys=True)).decode("utf-8")
    if session.query(models.ArchivedSchema).filter(models.ArchivedSchema.hash == hash).count():
        return hash

    aqs = models.ArchivedSchema()
    aqs.hash = hash
    aqs.schema = questionnaire
    session.add(aqs)

    return hash


def db_create_receivertip(session, receiver, internaltip, tip_key):
    """
    Create a receiver tip for the specified receiver
    """
    receivertip = models.ReceiverTip()
    receivertip.internaltip_id = internaltip.id
    receivertip.receiver_id = receiver.id
    receivertip.crypto_tip_prv_key = Base64Encoder.encode(tip_key)
    session.add(receivertip)
    return receivertip


def db_create_submission(session, tid, request, user_session, client_using_tor, client_using_mobile):
    encryption = db_get(session, models.Config, (models.Config.tid == tid, models.Config.var_name == 'encryption'))

    crypto_is_available = encryption.value

    context, questionnaire = db_get(session,
                                    (models.Context, models.Questionnaire),
                                    (models.Context.id == request['context_id'],
                                     models.Questionnaire.id == models.Context.questionnaire_id))

    answers = request['answers']
    steps = db_get_questionnaire(session, tid, questionnaire.id, None, True)['steps']
    questionnaire_hash = db_archive_questionnaire_schema(session, steps)

    crypto_tip_pub_key = ''

    receivers = []
    for r in session.query(models.User).filter(models.User.id.in_(request['receivers'])):
        if crypto_is_available:
            if r.crypto_pub_key:
                # This is the regular condition of systems setup on Globaleaks 4
                # Since this version, encryption is enabled by default and
                # users need to perform their first access before they
                # could receive reports.
                receivers.append(r)
            elif encryption.update_date != datetime_null():
                # This is the exceptional condition of systems setup when
                # encryption was implemented via PGP.
                # For continuity reason of those production systems
                # encryption could not be enforced.
                receivers.append(r)
                crypto_is_available = False
        else:
            receivers.append(r)

    if not receivers:
        raise errors.InputValidationError("Unable to deliver the submission to at least one recipient")

    if 0 < context.maximum_selectable_receivers < len(request['receivers']):
        raise errors.InputValidationError("The number of recipients selected exceed the configured limit")

    if crypto_is_available:
        crypto_tip_prv_key, crypto_tip_pub_key = GCE.generate_keypair()

    itip = models.InternalTip()
    itip.tid = tid
    itip.status = 'new'
    itip.crypto_tip_pub_key = crypto_tip_pub_key

    itip.progressive = db_assign_submission_progressive(session, tid)

    if context.tip_timetolive > 0:
        itip.expiration_date = get_expiration(context.tip_timetolive)

    if context.tip_reminder > 0:
        itip.reminder_date = get_expiration(context.tip_reminder)

    # Evaluate the score level
    itip.score = request['score']

    itip.tor = client_using_tor
    itip.mobile = client_using_mobile

    itip.context_id = context.id
    itip.enable_two_way_comments = context.enable_two_way_comments
    itip.enable_attachments = context.enable_attachments

    whistleblower_identity = session.query(models.Field) \
                                    .filter(models.Field.template_id == 'whistleblower_identity',
                                            models.Field.step_id == models.Step.id,
                                            models.Step.questionnaire_id == context.questionnaire_id).one_or_none()

    if whistleblower_identity is not None:
        itip.enable_whistleblower_identity = True

    receipt = GCE.generate_receipt()
    itip.receipt_hash = GCE.hash_password(receipt, State.tenants[tid].cache.receipt_salt)

    session.add(itip)
    session.flush()

    # Evaluate if the whistleblower tip should be encrypted
    if crypto_is_available:
        crypto_tip_prv_key, itip.crypto_tip_pub_key = GCE.generate_keypair()
        wb_key = GCE.derive_key(receipt.encode(), State.tenants[tid].cache.receipt_salt)
        wb_prv_key, wb_pub_key = GCE.generate_keypair()
        itip.crypto_prv_key = Base64Encoder.encode(GCE.symmetric_encrypt(wb_key, wb_prv_key))
        itip.crypto_pub_key = wb_pub_key
        itip.crypto_tip_prv_key = Base64Encoder.encode(GCE.asymmetric_encrypt(wb_pub_key, crypto_tip_prv_key))

    # Apply special handling to the whistleblower identity question
    if itip.enable_whistleblower_identity and request['identity_provided'] and answers[whistleblower_identity.id]:
        if crypto_is_available:
            wbi = base64.b64encode(GCE.asymmetric_encrypt(itip.crypto_tip_pub_key, json.dumps(answers[whistleblower_identity.id][0]).encode())).decode()
        else:
            wbi = answers[whistleblower_identity.id][0]

        answers[whistleblower_identity.id] = ''

        db_set_internaltip_data(session, itip.id, 'whistleblower_identity', wbi)

    if crypto_is_available:
        answers = base64.b64encode(GCE.asymmetric_encrypt(itip.crypto_tip_pub_key, json.dumps(answers, cls=JSONEncoder).encode())).decode()

    db_set_internaltip_answers(session, itip.id, questionnaire_hash, answers)

    for uploaded_file in user_session.files:
        if not itip.enable_attachments:
            break

        if crypto_is_available:
            for k in ['name', 'type', 'size']:
                uploaded_file[k] = base64.b64encode(GCE.asymmetric_encrypt(itip.crypto_tip_pub_key, str(uploaded_file[k])))

        new_file = models.InternalFile()
        new_file.tid = tid
        new_file.id = uploaded_file['filename']
        new_file.name = uploaded_file['name']
        new_file.content_type = uploaded_file['type']
        new_file.size = uploaded_file['size']
        new_file.internaltip_id = itip.id
        new_file.reference_id = uploaded_file['reference_id']
        session.add(new_file)

    for user in receivers:
        if crypto_is_available:
            _tip_key = GCE.asymmetric_encrypt(user.crypto_pub_key, crypto_tip_prv_key)
        else:
            _tip_key = b''

        db_create_receivertip(session, user, itip, _tip_key)

    db_log(session, tid=tid, type='whistleblower_new_report')

    return {'receipt': receipt}


@transact
def create_submission(session, tid, request, user_session, client_using_tor, client_using_mobile):
    return db_create_submission(session, tid, request, user_session, client_using_tor, client_using_mobile)


class SubmissionInstance(BaseHandler):
    """
    The interface to perform a submission
    """
    check_roles = 'whistleblower'

    def post(self):
        """
        Perform a submission
        """
        request = self.validate_request(self.request.content.read(), requests.SubmissionDesc)

        return create_submission(self.request.tid,
                                 request,
                                 self.session,
                                 self.request.client_using_tor,
                                 self.request.client_using_mobile)
