# Features
* Allow multiple jobs to run. Importing a KML should be possible while other jobs running.
* Allow killing jobs
* Allow removing old/any jobs and their data.

# Big features
* Allow exporting data: KML + DEM + previews

# Issues
* Large WebP are slow in the browser, either intelligently downscale or tile the data? WebGPU?
* Resizing the map causes the tracks and webp previews to be out of sync for a second while the tracks are resized. Ideally zooming & panning in the map window are smooth.
* If the server is force-killed so running and pending jobs are in-complete. These jobs should be marked as killed/canceled instead of their last status.
* Drawing a rectangle and using "Use bounds for AIO" are not visually the same so they likely are different code "paths". "Use bounds for AIO" should just create a regular rectangle selection.
* Allow removing drawn selections? Drawn tracks? The map UI is a bit incomplete.
* Map UI "Finish" icon/tool doesn't seem to do anything.
* AIO build mosaic uses browser alert dialog instead of native HTTP prompt.
* Errors are in a nearly 0 height box in the side bar.
* AIO still asks for name, DEM should just use name of the container. Seperate name not needed.
* AIO prompt for name does not allow spaces. Again this name is not needed, and container "NAME" should be any valid string.
* Allow collapsing the "feature preview" list under the DEM. Clearly indicate the "KML" vs "DEM" section
* Dem "Use as DEM" should be redundent - DEM operations are all listed under.
* The previews are shifted between different ESGI mapping things and have slight black border; ideally we natively operate on the same format to avoid translating that causes this slight mis-match with the original DEM area. OR at least map that area outside to alpha channel. 
