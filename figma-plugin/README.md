# OpenTechJobs components, as a Figma plugin

Figma's REST API cannot create layers, so the components are built by a
plugin that runs inside your file. It reads nothing from the network:
every colour, size and face is written into code.js from DESIGN.md and
style.css.

1. Figma desktop, any file: Plugins > Development > Import plugin from
   manifest, pick this folder's manifest.json.
2. Plugins > Development > OpenTechJobs components.

It adds a page called "OpenTechJobs" with the paint styles (light and
dark), the text styles, one component per piece of the board (buttons,
fields, chip, view switch, badge, top bar, job row, metric tile, panel)
and a 1440px Board frame assembled from instances of them.

Fonts: it asks for Overused Grotesk, Source Sans 3 and Helvetica Neue,
the site's own, and falls back to Inter for any you do not have
installed. Install the two self-hosted ones from frontend/fonts first
for the real thing.
