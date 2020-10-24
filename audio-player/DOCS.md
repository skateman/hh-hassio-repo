# Home Assistant Add-on: audio-player

This add-on is a background daemon that plays any file or URL passed to its standard input. It was inspired by [Dingle's idea](https://community.home-assistant.io/t/hassio-and-local-audio/22975/14).

## Installation
The installation of this add-on is pretty straightforward and not different in comparison to installing any other Home Assistant add-on.

1. Enable the Home Hub repository in the Supervisor add-on store.
2. Search for the "audio-player" add-on in the Supervisor add-on store and install it.
3. Select your audio output device and hit Save on that as well.
4. Start the "audio-player" add-on.
5. Check the logs of the "audio-player" to see if everything went well.
6. Ready to go!

## Configuration
There are no configuration parameters for this add-on, the only thing to set up is the audio output device.

## Usage
The add-on can be triggered by calling the `hassio.addon_stdin` service with the following two attributes:
```yaml
service: hassio.addon_stdin
data:
  addon: 5b84fcb2_audio_player
  input: http://example.com/test.wav
```

Where the `addon` is the name of the add-on's container and the `input` is a URL or a filesystem path pointing to a WAV file. The add-on has access to the `/share` directory, so it's recommended to store frequently played notification sounds there.

## License

MIT License

Copyright (c) 2020 Dávid Halász

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
