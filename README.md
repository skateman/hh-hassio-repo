# HomeHub Home Assistant Add-ons Repository

This is a collection of a few custom add-ons for Home Assistant running on [HomeHub](https://www.reddit.com/r/raspberry_pi/comments/hvss03/i_designed_and_built_a_home_automation_hub_with/).

## Installation
The add-on containers are available through [GitHub Packages](https://github.com/features/packages) which does not allow unauthorized access. So in order to be able to pull the containers, you need to have a GitHub account and create a [personal access token](https://docs.github.com/en/free-pro-team@latest/github/authenticating-to-github/creating-a-personal-access-token) with the `read:packages` scope checked. Then you have to configure the Home Assistant Supervisor to use this token, either via the [frontend](https://www.home-assistant.io/blog/2020/10/21/supervisor-249/#private-container-registries) in advanced mode or via the API for e.g. curl:

```bash
curl -XPOST \
  -d '{"docker.pkg.github.com": { "username": "GH_USERNAME", "password": "GH_TOKEN" }}' \
  -H "Authorization: Bearer HASS_TOKEN" \
  -H "Content-Type: application/json"\
  http://HOMEASSISTANT_URL:8443/api/hassio/docker/registries

```
Note that for the API request you will need to create a Long Lived Access Token [on your account profile page](https://www.home-assistant.io/docs/authentication/#your-account-profile).

After you can authenticate against `docker.pkg.github.com`, you can add the repository the [same way as you would add any other add-on repository](https://www.home-assistant.io/hassio/installing_third_party_addons/) but using the following URL:
```
https://github.com/skateman/hh-hassio-repo
```

## License
MIT License

Copyright (c) 2020 Halász Dávid

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
