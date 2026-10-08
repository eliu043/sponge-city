### Overview

I am making a sandbox in the style of the course simmodeltwin.net. There should be more information in the agents.md about what this specifically means, but in short it is a web-based simulation that contains some data, some rule for how to apply that data, a clock for running the rule forward in time, and some sliders that will let us change the variables and assumptions in the dataset. If an agents.md with more context is missing from this session, please do not complete this prompt, and direct the person running this to tutorial 4. 

### data

In data/Original, you can find:
- Impervious_Surface -> this is the impervious surface data for each individual borough
- NYC_Planimetric_Database__Elevation_Points_20261007.csv contains the elevational data needed for the cellular automata logic

Any new processed datasets should go in data/Processed. 

### objectives

This is a simplified cellular-automata simulation of rainfall moving across and urban surface. The model should show how street elevations and imperviousness affect where water accumulates, how it moves between neighboring cells, and how it drains after rainfall stops.

### data prep - the rule

When processing the data, please note that the elevation dataset includes building, water, and spot elevations. Do not use all records together; instead, use SUB_CODE = 300000 to make sure we're only including spot-elevation records.

The published elevation values are in feet, keep all data (or convert) to ft. The planimetric documentation is available at: https://github.com/CityOfNewYork/nyc-planimetrics/blob/main/Capture_Rules.md

Create a regular square grid covering the area, start with cells measuring 50ft x 50ft.

Each processed cell should contain:

| field | meaning |
|---|---|
| `cell_id` | unique identifier |
| `row` | grid row |
| `column` | grid column |
| `geometry` | cell polygon |
| `centroid_x` | cell-center x coordinate |
| `centroid_y` | cell-center y coordinate |
| `elevation_m` | interpolated elevation at the cell center |
| `elevation_source_count` | number of source points near the cell |
| `nearest_elevation_point_m` | distance to the nearest source elevation point |
| `elevation_confidence` | high, medium or low |
| `impervious_fraction` | value from 0 to 1 |
| `neighbor_ids` | IDs of adjacent cells |
| `is_boundary` | whether the cell touches the study boundary |
| `water_depth_mm` | initial value of 0 |

Each cell can interact with as many as eight surrounding cells.

For the elevation points, they are samples of a surface. If it is computatationally efficient, feel free to build one during the preprocessing phase. A paper mentioned that TIN interpolation is ideal or inverse-distance weighting; the interpolated elevation can be assigned to the center of the grid cell. Do not assign every cell the value of its nearest point without interpolation to avoid creating stepped terraces.

Additionally:
- count source points within a reasonable search distance and calculate distance to the nearest source point
- if data is sparse, we can assign a confidence score for spot elevations
- tag any cells that fall outside the interpolation area and mark as a filter option

For imperviousness, calculate the fraction of the grid cell covered by the impervious land. Store the new data as a scale like:

0.0 = completely pervious
1.0 = completely impervious

If any records are missing, flag those cells in a similar manner to the confidence of elevation data.

Export the processed grid as a compact format appropriate for the application. Since this needs to be self-contained without referencing any external data sources, make sure that this is stored locally. On that note, please make sure that all data sources are contained and we're not pulling from mapbox or OSM or some other map provider. We can leave out the base layer if necessary and draw an outline of New York City

The preprocessing workflow must be reproducible, do not manually edit the processed data.

Every cell has fixed attributes:

cell_id
elevation_ft
impervious_fraction
neighbor_ids
elevation_confidence

Every cell also has a state that changes during the simulation:

water_depth_in

At the beginning of every run:

water_depth_in = 0

The simulation should use one-minute steps. At each step, each cell:

- received effective rainfall;
- loses water through an assumed drainage rate
- compares its water level with neighboring cells
- sends part of its movable water to neighbors with lower levels
- receives water sent by higher neighbors

it's important to use surface-water elevation and not just the ground elevation alone here. This allows a low cell to fill and eventually spill into neighboring cells.

water_level_ft =
    elevation_ft
    + water_depth_in / 1000

The storm severity of rainfall control determines rainfall intensity in inches per hour.

Convert rainfall to each one-minute timestep:

rainfall_per_step_in =
    rainfall_in_per_hour / 60

Use imperviousness data to estimate how much rainfall becomes surface water:

runoff_factor = pervious_runoff_fraction + (1 - pervious_runoff_fraction) × impervious_fraction

We can use this initial assumption:

pervious_runoff_fraction = 0.15

Then:

effective_rain_in = rainfall_per_step_in × runoff_factor

The value `0.15` is an explicit modeling assumption. Keep it in a configuration file and expose it in an advanced assumptions section if practical. Do not subtract a separate infiltration value after applying this runoff factor. The runoff factor already represents rainfall that does not infiltrate.

Apply a uniform assumed drainage rate:
drainage_per_step_in =
    drainage_in_per_hour / 60

Then:

drained_mm =
    min(water_depth_in, drainage_per_step_in)

Use this initial default:

drainage_in_per_hour = 10

Clearly label it as an assumption for now.

For the cell-to-cell routing,

After adding rainfall and subtracting drainage, calculate the water level of each cell. For neighbors with a lower water leve:

head_difference = current_cell_water_level - neighbor_water_level

ignore neighbors where the difference is zero or negative. all a cell to retain a small amount of surface water so that it is not immediately routed

surface_storage_in = 2

To calculate movable water:

moveable_water_in = max(0, water_depth_in - surface_storage_in); you can use an initial routing_fraction of 0.25

The maximum amount leavin the cell during one timestep is:

outgoing_water_in = movable_water_in x routing_fraction

Distribute this amount among lower neighboring cells according to their relative head differences:

neighbor_weight = head_difference / sum_of_all_positive_head_differences

Then:

flow_to_neighbor_in = outgoing_water_in x neighbor_weight

If a cell has no lower neighbor, it retains the water until drainage factors remove it or its rising water level allow it to spill to the next cell. The routing fraction and surface storage value are assumptions.

For assumptions in this model, keep them easy to change as sliders.

Make sure cells are updated together and not one at a time. For each timestep:

- calculate the rainfall and drainage for all cells
- calculate outgoing transfers
- collect incoming transfers in a separate array
- update all cell simultaneously

Water depth should never be negative

There is also the question of dealing with boundaries or edge conditions. For those cells, track water that leaves the study area as boundary_outflow_in. The water balance can be tracked at each timestep as rain_added = water_stored + water_drained + boundary_outflow

### the run

The default run can be:

storm duration: 60 minutes
drainage period after storm: 120 minutes
total run: 180 minutes
timestep: 1 minute

During the first 60 minutes, rainfall is added according to the selected storm severity. After minute 60, rainfall = 0; water should continue moving and draining until the clcok reaches minute 180.

### the interactive

The interface should have three primary regions:

- a left information panel;
- a large central map;
- a right panel containing controls, the clock and the legend.

The main controls should be:

A slider controlling rainfall intensity:

minimum: 0 in/hour
maximum: 100 in/hour
default: 54 in/hour

Show the numeric value beside the slider.

A slider controlling the assumed drainage rate:

minimum: 0 in/hour
maximum: 30 in/hour
default: 10 in/hour

Please include a time slider where the user can run, pause, reset, step backward/forward, and control playback-speed. indicate if the state is raining or draining.

For the map:

Display the processed grid over a restrained map or geographic outline. Keep the visual language clean, academic and minimal rather than dashboard-like. Represent water_depth_in through a white-to-blue color scale where dry cells are transparent or nearly white/white, shallow water should be pale blue, and greater depts should become progressively darker blue. The map should be updated during playback, the cell shoudl fade toward white or transparent as water routes away or drains. Include a legend showing water depth in inches.

Allow the user to hover over or click a cell to inspect important data. It may be helpful to show a small summary of data:

current minute
rainfall intensity
total surface water
total drained water
boundary outflow
maximum cell depth

Title is "The City As Sponge"

Okay, that is all, please let me know if you have any questions or if anything is not clear.