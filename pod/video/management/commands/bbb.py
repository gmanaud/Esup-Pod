"""
This command is used to manage the recordings made by BigBlueButton.
To achieve this, this command performs the following tasks :

- Connect to BBB / Scalelite server to get informations about the
current meetings and save then in Pod database.
This is useful to obtain the actuel meetings
and the moderators list of theses meetings.
Be careful : in BBB, we only have the firstname and last name
of these moderators.

- Search for recordings available for meetings.
Search for meetings, made since 4 days, with their presentation recorded
where the recording is not available for the moment.
The idea of the 4 days is to avoid to process recordings that were deleted
or with bad data in the database (in fact, the recording tag in BBB
seems always true even if not recorded).

- Search to matching BBB users as Pod users.
This allows to try if BBB user (known with firstname and lastname) is matching
a Pod user. You can parameter the BBB username format via the use
of BBB_USERNAME_FORMAT setting.
At each use of this script, we search to matching BBB users
- not already known - as Pod users.
Be careful : tested with the Moodle plugin, mod_bigbluebuttonbn,
not with Greenlight (should be the same if use of LDAP with givenName
and lastName).

- Then, we check directory (DEFAULT_BBB_PATH) to publish video files that were
generated by bbb-recorder (DEFAULT_BBB_PLUGIN). If video files found, this
script encode them as Pod video.

Finally, if there was at least one error, an email is sent to Pod admins.

This script must be executed regurlaly (for an example, with a CRON task).
Example : crontab -e */5 * * * * /usr/bin/bash -c 'export
WORKON_HOME=/data/www/%userpod%/.virtualenvs; export
VIRTUALENVWRAPPER_PYTHON=/usr/bin/python3.6; cd
/data/www/%userpod%/django_projects/podv2; source
/usr/bin/virtualenvwrapper.sh; workon django_pod; python manage.py bbb
main' """
import os
import traceback
from django.utils import translation
from django.core.management.base import BaseCommand
from django.conf import settings
from pod.bbb.models import Meeting
from pod.bbb.models import User as BBBUser
import hashlib
import requests
import datetime
import dateutil.parser
from django.core.mail import mail_admins
from django.utils import timezone
from xml.dom import minidom
import urllib.parse

from pod.video.models import Video, Type, get_storage_path_video
from pod.video.encode import start_encode
from django.contrib.auth.models import User

from django.db.models import Value
from django.db.models.functions import Concat

LANGUAGE_CODE = getattr(settings, "LANGUAGE_CODE", 'fr')

# Directory that will contain the video files generated by bbb-recorder
DEFAULT_BBB_PATH = getattr(
    settings, 'DEFAULT_BBB_PATH',
    "/data/bbb-recorder/media/"
)
# The last caracter of DEFAULT_BBB_PATH must be an OS separator
if not DEFAULT_BBB_PATH.endswith(os.path.sep):
    DEFAULT_BBB_PATH += os.path.sep

# BigBlueButton or Scalelite server URL, where BBB Web presentation and API are
BBB_SERVER_URL = getattr(
    settings, 'BBB_SERVER_URL',
    "https://bbb.univ.fr/"
)
# The last caracter of BBB_SERVER_URL must be /
if not BBB_SERVER_URL.endswith("/"):
    BBB_SERVER_URL += "/"

# BigBlueButton key or Scalelite LOADBALANCER_SECRET
BBB_SECRET_KEY = getattr(
    settings, 'BBB_SECRET_KEY',
    ""
)
# Default type of the generated video
DEFAULT_BBB_TYPE_ID = getattr(
    settings, 'DEFAULT_BBB_TYPE_ID',
    1
)
# Username format of the user in BBB
BBB_USERNAME_FORMAT = getattr(
    settings, 'BBB_USERNAME_FORMAT',
    "first_name last_name"
)

# Allowed extensions
VIDEO_ALLOWED_EXTENSIONS = getattr(
    settings, 'VIDEO_ALLOWED_EXTENSIONS', (
        '3gp',
        'avi',
        'divx',
        'flv',
        'm2p',
        'm4v',
        'mkv',
        'mov',
        'mp4',
        'mpeg',
        'mpg',
        'mts',
        'wmv',
        'mp3',
        'ogg',
        'wav',
        'wma',
        'webm',
        'ts'
    )
)

# Mode debug (0: False, 1: True)
DEBUG = getattr(
    settings, 'DEBUG',
    False
)
# Encode video
ENCODE_VIDEO = getattr(settings,
                       'ENCODE_VIDEO',
                       start_encode)


def print_if_debug(str):
    if DEBUG:
        print(str)


def encode_file_exist(filename, extension, message_error, html_message_error):
    # A video file exist in the BBB directory : encode it
    print_if_debug(" - Encode BBB video file : " + filename)
    # Absolute path of the video
    source_file = os.path.join(DEFAULT_BBB_PATH, filename)

    # Filename corresponds to : internal_meeting_id.webm
    internalMeetingId = filename.replace("." + extension, "")
    # Check if the meeting already exists in Pod database
    oMeeting = Meeting.objects.filter(
        internal_meeting_id=internalMeetingId).first()
    if oMeeting:
        # Set video properties with meetng informations
        video = Video()
        video.title = oMeeting.meeting_name
        if oMeeting.encoded_by_id:
            video.owner = User.objects.get(id=oMeeting.encoded_by_id)
        video.type = Type.objects.get(id=DEFAULT_BBB_TYPE_ID)
        video.date_evt = oMeeting.session_date
        # Video management
        storage_path = get_storage_path_video(
            video, os.path.basename(source_file))
        dt = str(datetime.datetime.now()).replace(":", "-")
        nom, ext = os.path.splitext(os.path.basename(source_file))
        ext = ext.lower()
        # Video name
        video_name = nom + "_" + dt.replace(" ", "_") + ext
        video.video = os.path.join(
            os.path.dirname(storage_path), video_name)
        # Move source file to destination
        os.makedirs(os.path.dirname(video.video.path), exist_ok=True)
        os.rename(source_file, video.video.path)
        video.save()
        # Encode
        ENCODE_VIDEO(video.id)
    else:
        # Meeting was certainly deleted in Pod database
        print_if_debug(" - WARNING : It seems that this meeting was deleted "
                       "from Pod database. "
                       "internal_meeting_id : " + internalMeetingId)

    return html_message_error, message_error


def process_directory(files, root, html_message_error, message_error):
    # Search files in the BBB directory
    for filename in files:
        # Name of the directory
        dirname = root.split(os.path.sep)[-1]
        print_if_debug("\n*** Process the file " + os.path
                       .join(DEFAULT_BBB_PATH, dirname, filename)
                       + " ***")
        # Check if extension is a good extension (videos extensions)
        extension = filename.split(".")[-1]

        valid_ext = VIDEO_ALLOWED_EXTENSIONS
        if not (extension in valid_ext and filename != extension):
            print_if_debug(
                " - WARNING : " + extension + "is not a valid video "
                "extension. If it should "
                "be, add it to the setting "
                "VIDEO_ALLOWED_EXTENSIONS")
            continue
        html_message_error, message_error = encode_file_exist(
            filename, extension, message_error, html_message_error)

    return html_message_error, message_error


def get_bbb_meetings_by_xml(html_message_error, message_error):
    print_if_debug("\n*** Check BBB/Scalelite actual meetings  ***")
    try:
        # See https://docs.bigbluebutton.org/dev/api.html#usage
        # for checksum and security
        checksum = hashlib.sha1(
            str("getMeetings" + BBB_SECRET_KEY).encode('utf-8')).hexdigest()
        # Request on BBB/Scalelite server (API)
        # URL example :
        # https://bbb.univ.fr/bigbluebutton/api/getMeetings?checksum=xxxx
        urlToRequest = BBB_SERVER_URL
        urlToRequest += "bigbluebutton/api/getMeetings?checksum=" + checksum
        addr = requests.get(urlToRequest)
        print_if_debug("Request on URL : " + urlToRequest + ""
                       ", status : " + str(addr.status_code))
        # XML result to parse
        xmldoc = minidom.parseString(addr.text)
        returncode = xmldoc.getElementsByTagName(
            "returncode")[0].firstChild.data
        # Management of FAILED error (basically error in checksum)
        if (returncode == "FAILED"):
            err = "Return code = FAILED for : " + urlToRequest
            err += " => : " + xmldoc.toxml() + ""
            message_error += err + "\n"
            html_message_error += "<li>" + err + "</li>"
        # Actual meetings
        meetings = xmldoc.getElementsByTagName("meeting")
        for meeting in meetings:
            get_meeting(meeting, html_message_error, message_error)

    except Exception as e:
        err = "Problem to parse XML meetings on the BBB/Scalelite server "\
            "or save in Pod database : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


def get_meeting(meeting, html_message_error, message_error):
    try:
        # Get meeting informations
        meetingName = meeting.getElementsByTagName(
            "meetingName")[0].firstChild.data
        meetingID = meeting.getElementsByTagName(
            "meetingID")[0].firstChild.data
        internalMeetingID = meeting.getElementsByTagName(
            "internalMeetingID")[0].firstChild.data
        date = meeting.getElementsByTagName(
            "createDate")[0].firstChild.data
        # Recording seems useless (~always True)
        recording = meeting.getElementsByTagName(
            "recording")[0].firstChild.data

        print_if_debug("\n - Meeting : " + internalMeetingID)

        # Id of the current meeting
        idActualMeeting = 0
        # Search if the meeting already exists in Pod database
        oMeeting = Meeting.objects.filter(
            internal_meeting_id=internalMeetingID).first()
        if oMeeting:
            idActualMeeting = oMeeting.id
            print_if_debug("   + Meeting already exists in Pod database.")
            # Check if meeting is recorded now
            if oMeeting.recorded is False and recording == "true":
                print_if_debug("   + Recording this meeting. ")
                oMeeting.recorded = True
                oMeeting.save()
        else:
            # Create the meeting in Pod database
            print_if_debug("   + Create the meeting in Pod database. "
                           "internal_meeting_id : " + internalMeetingID)
            meetingToCreate = Meeting()
            meetingToCreate.meeting_id = meetingID
            meetingToCreate.internal_meeting_id = internalMeetingID
            meetingToCreate.meeting_name = meetingName
            # Convert the date in the database format
            dateForSql = dateutil.parser.parse(date, ignoretz=False)
            meetingToCreate.session_date = dateForSql
            # Initially encoding_step = 0 (very important)
            meetingToCreate.encoding_step = 0
            # Recording tag seems ~always true, so seems useless
            if recording == "true":
                meetingToCreate.recorded = True
            else:
                meetingToCreate.recorded = False
            meetingToCreate.recording_available = False
            meetingToCreate.save()
            idActualMeeting = meetingToCreate.id

        # Management of the participants
        for attendee in meeting.getElementsByTagName("attendee"):
            get_attendee(attendee, idActualMeeting,
                         html_message_error, message_error)

    except Exception as e:
        err = "Problem to get BBB meeting "\
            "and save in Pod database : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


def get_attendee(attendee, idActualMeeting, html_message_error, message_error):
    try:
        # In BigBlueButton, we have only the full name
        # Full name format : "first_name last_name"
        fullName = attendee.getElementsByTagName(
            "fullName")[0].firstChild.data
        role = attendee.getElementsByTagName("role")[0].firstChild.data
        # We save only the BBB moderator
        if role == "MODERATOR":
            # Search if the BBB user already exists in Pod
            oBBBUser = BBBUser.objects.filter(
                full_name=fullName, meeting_id=idActualMeeting).first()
            if oBBBUser:
                print_if_debug("   + User already exists "
                               "in Pod database : "
                               "" + oBBBUser.full_name)
            else:
                # Create the meeting user in Pod database
                print_if_debug("   + Create the meeting user "
                               "in Pod database : " + fullName)
                bbbUserToCreate = BBBUser()
                bbbUserToCreate.full_name = fullName
                bbbUserToCreate.role = 'MODERATOR'
                bbbUserToCreate.meeting_id = idActualMeeting

                bbbUserToCreate.save()
    except Exception as e:
        err = "Problem to get BBB attendee "\
            "and save in Pod database : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


def matching_bbb_pod_user(html_message_error, message_error):
    print_if_debug("\n*** Search if BBB users matching to Pod users ***")
    try:
        # Search for BBB users already in Pod database, without matching
        # By security : take only the 500 last BBB users, to avoid process
        # too long. Usefull when users are not known in Pod.
        bbbUsers = BBBUser.objects.filter(
            user_id__isnull=True
        ).order_by('-id')[:500]

        # Use the BBB_USERNAME_FORMAT setting to make the matching.
        if BBB_USERNAME_FORMAT == "last_name first_name":
            bbbUsernameFormat = Concat('last_name', Value(' '), 'first_name')
        else:
            bbbUsernameFormat = Concat('first_name', Value(' '), 'last_name')

        for bbbUser in bbbUsers:
            # Search if this BBB user matching to a Pod user.
            # Take the first one (This can cause an error in case of namesake!)
            podUser = User.objects.annotate(
                name=bbbUsernameFormat,
            ).filter(name__icontains=bbbUser.full_name).first()
            if podUser:
                # Update the id and the username of this user
                print_if_debug(" - A Pod user matching a BBB user "
                               "was found in Pod database. "
                               "BBB user : " + bbbUser.full_name + ". "
                               "Pod user : " + podUser.username)
                bbbUser.username = podUser.username
                bbbUser.user_id = podUser.id
                bbbUser.save()
            else:
                print_if_debug(" - A Pod user matching a BBB user "
                               "was NOT found in Pod database. "
                               "BBB user : " + bbbUser.full_name)

    except Exception as e:
        err = "Problem to matching BBB user to Pod user : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


def get_bbb_meetings_recorded(html_message_error, message_error):
    print_if_debug("\n*** Check BBB meetings recorded in Pod, "
                   "not already available ***")

    try:
        # Search for meetings, made since 4 days, with their presentation
        # recorded where the recording is not available for the moment.
        # The idea of the 4 days is to avoid to process recordings that
        # were deleted or with bad data in the database.
        # For informations : parameter Recorded seems useless (~always True)
        dateSince4d = timezone.now() - timezone.timedelta(days=4)
        meetings = Meeting.objects.filter(recorded=True,
                                          recording_available=False,
                                          session_date__gte=dateSince4d
                                          ).order_by('id')
        for meeting in meetings:
            # Search recording on BBB/Scalelite server
            html_message_error, message_error = get_bbb_recording_by_xml(
                meeting.meeting_id, meeting.internal_meeting_id,
                html_message_error, message_error)

    except Exception as e:
        err = "Problem to get recorded meetings "\
            "in Pod database : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


def get_bbb_recording_by_xml(meeting_id, internal_meeting_id,
                             html_message_error, message_error):
    print_if_debug(" - Check BBB/Scalelite recording.")
    try:
        # See https://docs.bigbluebutton.org/dev/api.html#usage
        # for checksum and security
        uri = "getRecordingsmeetingID="
        uri += urllib.parse.quote_plus(meeting_id) + BBB_SECRET_KEY
        checksum = hashlib.sha1(str(uri).encode('utf-8')).hexdigest()
        # Request on BBB/Scalelite server (API)
        # URL example : https://bbb.univ.fr/bigbluebutton/api/getRecordings?
        # meetingID=xxxxxxxxxxxxxx&checksum=yyyyyyyyyyyyyyy
        urlToRequest = BBB_SERVER_URL
        urlToRequest += "bigbluebutton/api/getRecordings?meetingID="
        urlToRequest += urllib.parse.quote_plus(meeting_id)
        urlToRequest += "&checksum=" + checksum
        addr = requests.get(urlToRequest)
        print_if_debug("   + Request on URL : " + urlToRequest + ""
                       ", status : " + str(addr.status_code))
        # XML result to parse
        xmldoc = minidom.parseString(addr.text)
        returncode = xmldoc.getElementsByTagName(
            "returncode")[0].firstChild.data
        # Management of FAILED error (basically error in checksum)
        if (returncode == "FAILED"):
            err = "Return code = FAILED for : " + urlToRequest
            err += " => : " + xmldoc.toxml() + ""
            message_error += err + "\n"
            html_message_error += "<li>" + err + "</li>"
        # Actual recordings
        recordings = xmldoc.getElementsByTagName("recording")
        for recording in recordings:
            get_recording(recording, internal_meeting_id,
                          html_message_error, message_error)

    except Exception as e:
        err = "Problem to parse XML recording on the BBB/Scalelite server "\
            "or save in Pod database : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


def get_recording(recording, internal_meeting_id,
                  html_message_error, message_error):
    try:
        # Get recording informations
        # meetingID = recording.getElementsByTagName(
        #    "meetingID")[0].firstChild.data
        internalMeetingID = recording.getElementsByTagName(
            "internalMeetingID")[0].firstChild.data

        # We only process the correct recording,
        # set by the internal_meeting_id
        if internalMeetingID == internal_meeting_id:
            # Check if the meeting already exists in Pod database
            oMeeting = Meeting.objects.filter(
                internal_meeting_id=internal_meeting_id).first()
            if oMeeting:
                recording_url = ""
                for playback in recording.getElementsByTagName("playback"):
                    # Recording URL corresponds to the BBB presentation URL
                    recording_url = playback.getElementsByTagName(
                        "url")[0].firstChild.data
                    # We take the first thumbnail found
                    thumbnail_url = playback.getElementsByTagName(
                        "image")[0].firstChild.data

                if recording_url != "":
                    print_if_debug("   + The recording was found. "
                                   "internal_meeting_id : "
                                   + internal_meeting_id + ". "
                                   "recording_url : " + recording_url)
                    oMeeting.recording_available = True
                    oMeeting.recording_url = recording_url
                    oMeeting.thumbnail_url = thumbnail_url
                    oMeeting.save()
            else:
                # Meeting was certainly deleted in Pod database
                print_if_debug("   + WARNING : It seems that this "
                               "meeting was deleted from Pod database "
                               " : " + internalMeetingID)

    except Exception as e:
        err = "Problem to get BBB recording "\
            "and save in Pod database : " + str(e) + ". "\
            "" + traceback.format_exc()
        message_error += err + "\n"
        html_message_error += "<li>" + err + "</li>"
        print_if_debug(err)
        return html_message_error, message_error

    return html_message_error, message_error


class Command(BaseCommand):
    # First possible argument : main
    args = 'main'
    help = 'Manage the BigBlueButton presentation '
    valid_args = ['main']

    def add_arguments(self, parser):
        parser.add_argument('task')

    def handle(self, *args, **options):
        # Activate a fixed locale fr
        translation.activate(LANGUAGE_CODE)
        if options['task'] and options['task'] in self.valid_args:

            html_message_error = ""
            message_error = ""
            # Connect to BBB / Scalelite server to get infos
            # about the current meetings
            html_message_error, message_error = get_bbb_meetings_by_xml(
                html_message_error, message_error)

            # Search for recording available for  meetings
            html_message_error, message_error = get_bbb_meetings_recorded(
                html_message_error, message_error)

            # Search to matching BBB users as Pod users
            html_message_error, message_error = matching_bbb_pod_user(
                html_message_error, message_error)

            # Check directory to publish video files
            for root, dirs, files in os.walk(DEFAULT_BBB_PATH):
                if "logs" in dirs:
                    dirs.remove("logs")
                html_message_error, message_error = process_directory(
                    files, root, html_message_error, message_error)

            # If there was at least one error,send an email to Pod admins
            if message_error != "":
                print_if_debug(
                    "\n\n*** An email BBB job [Error(s) "
                    "encountered] was sent to Pod admins, with message : "
                    "***\n\n" + message_error)
                mail_admins("BBB job [Error(s) encountered]",
                            message_error, fail_silently=False,
                            html_message=html_message_error)
        else:
            print(
                "*** Warning: you must give some arguments: %s ***" %
                self.valid_args)
