# audio-powersave
Home Assistant Add-on to control the power saving capabilities of an amplifier or an external sound card.

## About

This add-on is a backgrond daemon that [subscribes to PulseAudio events](https://freedesktop.org/software/pulseaudio/doxygen/subscribe.html) and keeps a count of the sink inputs. Initially the number is zero and upon an increase it calls a user-specified API endpoint with a user-specified payload. If the number gets down to zero, it sets up a user-specified timeout and calls another user-specified API endpoint. With the right configuration it's possible turn off an amplifier if it's not used and turn it back on when necessary.
