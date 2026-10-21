# lf2resolve

Convert a **LumaFusion** project package (`.lfpackage`) into a timeline that
**DaVinci Resolve** can import, so you can rough-cut on your tablet or phone
and finish on the desktop.

## Requirements

Python 3.8 or newer. Nothing else to install.

## Usage

1. In LumaFusion, export the project as a package: Share/Export > Project Package.
   Copy the `.lfpackage` to your computer.
2. Run:

   ```sh
   python3 lf2resolve.py "My Project.lfpackage"
   ```

   This creates a `My Project (Resolve)` folder next to the package:

   ```
   My Project (Resolve)/
     My Project.xml   <- the timeline
     media/           <- the clips, extracted from the package
   ```

3. In Resolve, set the project's **timeline frame rate** to match what the
   tool prints (e.g. `@ 30 fps`). Then go to **File > Import > Timeline...** and
   choose the `.xml` file. Leave "Automatically import source clips into media
   pool" checked.

Keep the output folder where it is when you import, because the XML points to
the media by absolute path. If you move it, Resolve will ask you to relink and
the file names still match.

To see what the tool decoded without writing anything:

```sh
python3 lf2resolve.py "My Project.lfpackage" --inspect
```

Use `-o FOLDER` to choose the output location.

## What carries over

| LumaFusion                        | Resolve                                   |
|-----------------------------------|-------------------------------------------|
| Main track                        | V1 / A1                                   |
| Overlay tracks                    | V2, V3... (with their audio on A2, A3...) |
| Audio-only tracks                 | Additional audio tracks                   |
| Clip position, trims, gaps        | Exact (to the frame)                      |
| Transitions                       | Cross Dissolve + audio cross fade         |
| Clip volume / pan                 | Clip volume / pan                         |
| Clip opacity                      | Opacity                                   |
| Hidden / locked tracks            | Disabled / locked tracks                  |

Not converted yet: titles, effects, colour, keyframes, speed changes, reversed
clips, position/scale/crop, markers and multicam. The tool lists every such
item it finds under **"Not carried over / check in Resolve"**, so you know what
to rebuild.

Every LumaFusion transition style (Zoom Blur, Push, etc.) becomes a cross
dissolve of the same length. LumaFusion stores a transition as its own
slot between two clips. The tool turns it into a transition centred on the cut,
with each clip lending half the duration from its handles.

## Limitations

- **One way only.** LumaFusion can't import timelines, so changes made in
  Resolve can't be sent back.
- The project format is undocumented. This tool was built from LumaFusion 3.0
  project files, and other versions may differ. If a conversion looks wrong,
  run `--inspect` and compare it with the LumaFusion timeline.

## Development

```sh
python3 -m unittest discover -s tests
```
