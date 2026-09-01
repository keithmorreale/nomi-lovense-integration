# # # # # # # # # # # # #
# # command_parser.py # #
# # # # # # # # # # # # #

import re


DEFAULT_STRENGTH = 10
DEFAULT_DURATION = 20
MIN_STRENGTH = 1
MAX_STRENGTH = 20
MAX_DURATION = 60

PRESET_WORDS = ("pulse", "wave", "fireworks", "earthquake")


def _normalize_strength(value):
    """
    Return a valid Lovense intensity from 1-20.
    Invalid values fall back to DEFAULT_STRENGTH.
    """
    try:
        strength = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STRENGTH

    if MIN_STRENGTH <= strength <= MAX_STRENGTH:
        return strength

    return DEFAULT_STRENGTH


def _normalize_duration(value):
    """
    Return a safe command duration.

    Defaults to DEFAULT_DURATION.
    Values are limited to 1-60 seconds.
    """
    if value is None:
        return DEFAULT_DURATION

    try:
        duration = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DURATION

    if 1 <= duration <= MAX_DURATION:
        return duration

    return DEFAULT_DURATION


def _parse_device_payload(payload):
    """
    Parse the contents of a single [DEVICE: ...] tag.

    Supported examples:

        [DEVICE: vibrate 7]
        [DEVICE: buzz 12]
        [DEVICE: shake 4]
        [DEVICE: vibrate 15, 8s]

        [DEVICE: fireworks]
        [DEVICE: wave]
        [DEVICE: pulse]
        [DEVICE: earthquake]

        [DEVICE: stop]
    """

    payload = payload.strip().lower()

    # ---------------------------------------------------------
    # STOP
    # ---------------------------------------------------------

    if re.fullmatch(r'(stop|halt|cease)', payload):
        return {
            'action': 'Stop',
            'timeSec': 0,
        }

    # ---------------------------------------------------------
    # VIBRATION COMMAND
    # ---------------------------------------------------------

    vibrate_match = re.fullmatch(
        r'(vibrate|buzz|shake)'
        r'(?:\s+(?:at\s+|level\s+|intensity\s+)?(\d{1,2}))?'
        r'(?:\s*,?\s*(\d{1,3})\s*(?:s|sec|secs|second|seconds))?',
        payload,
        re.IGNORECASE,
    )

    if vibrate_match:
        strength = _normalize_strength(vibrate_match.group(2))
        duration = _normalize_duration(vibrate_match.group(3))

        return {
            'action': f'Vibrate:{strength}',
            'timeSec': duration,
        }

    # ---------------------------------------------------------
    # PRESET COMMAND
    # ---------------------------------------------------------

    preset_match = re.fullmatch(
        r'(?:preset\s+)?(pulse|wave|fireworks|earthquake)'
        r'(?:\s*,?\s*(\d{1,3})\s*(?:s|sec|secs|second|seconds))?',
        payload,
        re.IGNORECASE,
    )

    if preset_match:
        preset_name = preset_match.group(1).lower()
        duration = _normalize_duration(preset_match.group(2))

        return {
            'action': f'Preset:{preset_name}',
            'timeSec': duration,
        }

    return None


def parse_nomi_commands(response_text):
    """
    Extract explicit device commands from a Nomi response.

    ONLY text inside [DEVICE: ...] tags is interpreted as a
    Lovense command.

    Normal conversational text is ignored.

    Example:

        "I grin at you.
         [DEVICE: fireworks]
         Maybe we should make this stronger.
         [DEVICE: vibrate 12, 8s]"

    Returns:

        [
            {'action': 'Preset:fireworks', 'timeSec': 20},
            {'action': 'Vibrate:12', 'timeSec': 8}
        ]
    """

    if not response_text:
        return []

    device_pattern = re.compile(
        r'\[\s*DEVICE\s*:\s*(.*?)\]',
        re.IGNORECASE | re.DOTALL,
    )

    commands = []

    for match in device_pattern.finditer(response_text):
        payload = match.group(1)

        command = _parse_device_payload(payload)

        if command:
            commands.append(command)

    return commands


def parse_nomi_response(response_text):
    """
    Backward-compatible single-command interface.

    Returns the first valid explicit [DEVICE: ...] command,
    or None when no command tag exists.
    """

    commands = parse_nomi_commands(response_text)

    if commands:
        return commands[0]

    return None
