"""Document Intake: the limits the site and the agent plugin must agree on.

``reader`` renders pages under these and ``client`` refuses a request over them, so the
two cannot drift apart. The plugin's vision-task route enforces the same numbers: change
them here and there together.
"""

# Pages rendered from one PDF.
MAX_PAGES = 10
# Images in one vision-task request.
MAX_IMAGES = 10
# Bytes per image: under chat's 240 KB cap so the base64 request stays small.
IMAGE_BYTES = 200_000
# The plugin gives up at 105 s; the site waits longer so the plugin's own 504 is the
# error the uploader's row reports, not a bare transport timeout.
SITE_WAIT_S = 110
