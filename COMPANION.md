# Feature/TODO list
* Add button to main page (when online) to force re-sync of app (invalidate all JS/HTML PWA cache)? Faster than closing and re-opening PWA.
* Also show sunrise/sunset time with weather, and moon phase state.
* Allow dismissing map Layers dialog from bottom of open layers dialog.
* KML node/marker locations are not scalled correctl when zooming out, look correct when zoomed in.
* Sometimes scrolling tries to scroll the entire page istead of just the description/rappel content; not sure why since the PWA is fixed size.
* Viewport fix for cuttoff/blurred top edge causes a small ~2mm gap in content that looks weird.
* To companion page, add additional button/page for "start approach/canyon", "end approach", "start descent" "end descent", "start exit", "end exit/canyon"
* * This data should be in exported KML with log of GPS points we have. Need some fuzzy logic points -> paths; a 30 minute GPS block window should not show up as a straight line
* * * That said, 30 minutes with no movement (e.g lunch) is fine; just large time+movement delta should be assumed to have followed KML track - not a stright line. 
* Download low-res (~100MB) USA wide background map.
* Allow adjusting "3D Terrain" from 0.5 to 3x.
