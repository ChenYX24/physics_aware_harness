# ICLR Core80 Rigid + Cloth Roster — Approved Working Roster

Chinese review copy: [`ICLR_CORE80_ROSTER_REVIEW_ZH.md`](ICLR_CORE80_ROSTER_REVIEW_ZH.md)

## Status and allocation

The machine registry now reflects this approved working roster. This does not freeze unauthored CaseSpecs, acquire assets, or count any run as evaluated. F001–F010 and A001–A010 remain unchanged.

| Domain | Count |
|---|---:|
| UE rigid body | 68 |
| Taichi cloth + UE replay | 12 |
| **Core80** | **80** |

| Difficulty | Count | Share |
|---|---:|---:|
| E — atomic/calibration | 19 | 23.75% |
| M — combined | 30 | 37.5% |
| H — multi-stage/multi-body | 31 | 38.75% |

M+H is 76.25%; E:M:H is `19:30:31`.

The revision preserves every retained original Core100 case at its original ID and relative position. The 18 removed low-value, redundant, or superseded rows are `R002/R007/R008/R017/R021/R022/R028/R032/R034/R039/R042/R046/R055/R059/R062/R064/R065/R080`. Six new complex rigid cases use `R081–R086`; cloth uses `C001–C012`. The nine retained rigid image-text IDs remain `R011/R016/R026/R036/R043/R048/R061/R077/R079`, so the unchanged fluid/articulated rows keep Core100 at exactly 15 image-text cases.

## Capability boundary

- Rigid cases use qualified UE collision/contact, friction, rolling/spin, constraints/drives, unilateral distance spring, kinematic translation, continuous force, aerodynamic wind, signed radial field, and energy-gated fracture.
- Magnetic cases use the qualified idealized signed inverse-square radial field. They do not claim dipole orientation, induction, hysteresis, or magnetic torque.
- The qualified cloth baseline is one fixed-topology mass-spring cloth, gravity, optional one fixed edge, multiple analytic one-way colliders, prescribed world-transform motion, and shared wind; no tearing, self-collision, arbitrary-mesh collision, or two-way coupling is required.
- Real assets are preferred as visible shells; explicit analytic geometry remains the auditable physics truth where appropriate.

The cloth runtime now consumes multiple one-way analytic `sphere/box/cylinder/plane` colliders, including prescribed position/rotation `kinematic_motion`, and replays their declared assets and trajectories in UE. G2 is also qualified: a standalone validated baker transfers selected transforms from a completed UE trajectory into those colliders before Taichi solve and synchronized UE replay. Cloth back-reaction is deliberately excluded. R085's compound constraint composition and R086's general fixed-waypoint path-length spring are both qualified by real-UE Smokes. None of these paths route on a Case ID.

Direct parameter-comparison rows are intentionally visible rather than hidden in ablations: R003 (restitution), R029/R040 (shape and rotational inertia), R030 (static friction), R060 (damping), R066 (slip threshold), and R072 (mass/drag). R076/R077 form a paired below/above-fracture-threshold control.

For the whole roster, background dressing does not count as physical complexity. Each M/H row should have either at least three visible participating bodies or at least two causally ordered physical stages. Calibration E rows may remain simpler.

## Proposed roster

`Local` means existing downloaded/Catalog content and is always searched first. `Meshy` is used only for a missing recognizable hero asset. `Proc` means generated physics geometry or a hidden collision body.

### Rigid body (68)

| ID | Case | Physics evidence | D | Scene | Asset plan | Adjustable variables |
|---|---|---|:---:|---|---|---|
| R001 | Ball bounces down five steps | ordered contacts; decaying rebound | M | Empty Play Room | Local/Proc ball; Proc textured steps | launch speed; restitution; step height |
| R003 | Rubber ball versus clay ball drop | restitution contrast | E | Wrestling Gym | Local/Proc textured balls | restitution pair; drop height |
| R004 | Oblique ball hits floor and a wooden wall, then knocks over an enlarged can | ordered contacts and rebound direction | E | Empty Workshop | Textured ball; wooden wall/floor; Local WH can | launch velocity; wall restitution; can mass |
| R005 | Ball rolls off a table, lands on a rug, then tips a crate with an off-center hit | support loss; ballistic motion; eccentric impact | M | Wrestling Gym | Local table, carpet and WH crate; Local/Proc ball | roll speed; table height; ball mass; impact offset |
| R006 | Wrecking ball collapses a block wall | hinge arc; impact; many-body collapse | H | Interior Construction | Meshy wrecking-ball shell/frame; Proc blocks | release angle; ball mass; block friction |
| R009 | Four colored balls enter an open basket along distinct trajectories | multiple arcs; rim contact; ball collision; containment | M | Empty Play Room | Meshy open basket; textured balls; analytic collision | launch speed/angle; release time; lateral offset |
| R010 | Pendulum ball strikes a block that slides into a second target | constraint arc; ordered contacts; dissipative target slide | M | Wrestling Gym | Local table; existing apparatus and blocks | release angle; bob mass; block mass/friction; target gap |
| R011 | Barrel rolls off a table | rolling; support loss; ground contact | M | Empty Workshop | Local WH barrel + HI table | initial angular speed; barrel mass; table friction |
| R012 | Ramp ball jumps a gap and knocks a block from a pedestal | rolling; projectile; secondary fall | H | Hangar Interior | Meshy track dressing; Proc collision | ramp angle/friction; gap width; ball speed |
| R013 | Twelve-domino straight chain | strict sequential contacts | E | Empty Warehouse | Proc/materialized dominoes | domino spacing; friction; trigger angular speed |
| R014 | S-curved domino chain ends by dropping a vase | sequential S-curve contacts; terminal fall | H | Ballroom | Reuse dominoes; Poly Haven ceramic vase | path amplitude/phase; spacing; vase mass |
| R015 | Bowling ball strikes ten pins | head-pin causality; multi-direction scatter | H | Bowling lane | Local qualified bowling pack | ball speed/spin; lane friction; pin restitution |
| R016 | Fifteen-ball billiards break | multi-ball momentum and identity | H | Billiard Hall | Catalog-first/Meshy table and cue; analytic rails | cue speed/angle; ball restitution; cloth friction |
| R018 | Five-ball Newton cradle | constrained impulse transfer | H | Ballroom | Meshy cradle frame; analytic balls/strings | release angle; restitution; joint damping; ball gap |
| R019 | Bottom box of a three-box stack is struck and the top box hits a barrel | impact; delayed support loss; secondary contact | M | Warehouse composition | Local WH boxes and barrel | striker speed; mass ratio; stack friction |
| R020 | Ball knocks three blocks from a ledge in order | ordered contacts and falls | M | Empty Warehouse | Local blocks; Proc ball | ball speed; target spacing; restitution |
| R023 | Three unequal carts transfer impulse in order | ordered mass-ratio transfer | H | Aircraft Workshop | Catalog-first/Meshy reusable cart; WH ballast | cart masses; initial speed; rolling friction |
| R024 | Two-ball glancing collision | oblique momentum direction change | E | Billiard Hall | Reuse R016 balls/table | speed; impact offset; restitution |
| R025 | Rolling ball hits a vase and a block in order | ordered contacts and responses | H | HomeInterior | Local HI vase/block/table; Proc ball | ball speed; target spacing; surface friction |
| R026 | Ten marbles pass through a funnel | count; interior contacts; containment | H | HomeInterior | Meshy-first open funnel + Local bowl | marble size/count; funnel slope; wall friction |
| R027 | Solid cylinder rolls down a rough ramp into a stopper | translation/rotation consistency; terminal contact | E | University Workshop | Proc cylinder/ramp; Local beam stopper/materials | ramp angle; rolling friction; cylinder radius |
| R029 | Sphere and cube descend the same ramp | rolling-versus-sliding contrast | M | Empty Workshop | Local/Proc bodies and ramp | ramp angle; body radii; friction; mass |
| R030 | Two blocks with different friction slip at different board angles | ordered static-friction thresholds | M | HomeInterior | Local board and two visually distinct blocks | static-friction pair; tilt rate; block masses |
| R031 | Crate crosses low/high/low-friction strips | piecewise speed response | M | Empty Warehouse | Local WH crate; material strips | strip friction values/widths; initial speed |
| R033 | Cable spool transitions from ramp to floor and nudges a small crate | continuous rotation; deceleration; terminal contact | M | Warehouse composition | Local WH spool/crate; Proc ramp | ramp angle; spool inertia; rolling friction; crate mass |
| R035 | Coin spins, wobbles, and settles | spin decay; tilt growth; rest | H | HomeInterior | Catalog-first/Meshy coin | initial spin; coin aspect ratio; surface friction |
| R036 | Spinning top slows and falls | precession; decay; tip contact | H | HomeInterior | Meshy-first qualified top | initial spin; COM height; tip friction |
| R037 | Puck makes two bank contacts then hits target | ordered banks and final hit | H | Empty Play Room | Proc textured puck/rails | launch speed/angle; rail restitution; target position |
| R038 | Pushed crate crosses static-friction threshold | hold then slide under force | E | Warehouse composition | Local WH crate | force magnitude/duration; static friction; crate mass |
| R040 | Sphere and cylinder race down a ramp | inertia-dependent arrival order | M | Empty Workshop | Proc bodies; realistic materials | body radii/masses; ramp angle; friction |
| R041 | Equal blocks balance a seesaw | pivot constraint; static balance | E | Country Club | Catalog-first/Meshy seesaw | masses; lever arms; pivot damping |
| R043 | Overhanging book stack collapses and knocks a cup | center-of-mass failure; secondary contact | M | HomeInterior | Local books, cup and table | overhang offsets; book friction; cup distance |
| R044 | Loaded bridge loses its central support | kinematic removal; loaded multi-body collapse | H | Empty Warehouse | Local WH pallets, beam, support and crates | bridge span; load masses; withdrawal speed |
| R045 | Tall narrow block tips after impact and topples two cans | impact torque; tipping; secondary contacts | M | Warehouse composition | Local WH box/cabinet/cans; Proc ball | ball speed/offset; block aspect ratio; can spacing |
| R047 | Book stack receives a side nudge and displaces a small vase | propagation; partial slip; terminal contact | E | HomeInterior | Local books, vase and table | nudge impulse; book friction; initial overlap |
| R048 | Lower block is pulled from a six-block tower | delayed force; support loss; collapse | H | Wrestling Gym | Existing Pilot assets; enlarged hidden floor | pull force/duration; block friction; tower layout |
| R049 | Ball pyramid collapses after base-ball removal | support-graph change; settling | M | Empty Play Room | Local/Proc ball set | base withdrawal speed; ball friction/restitution; layers |
| R050 | Cup and book lag differently on an accelerating tray | kinematic trajectory; two frictional responses | M | HomeInterior | Local tray, cup and book | tray acceleration; cargo friction/mass pairs |
| R051 | Crate slides after a platform stops and reaches a bumper | kinematic stop; inertial continuation; terminal contact | M | Empty Warehouse | Local WH pallet, crate and beam bumper | platform speed/deceleration; crate friction; bumper distance |
| R052 | Ladder slides between wall and floor and knocks a bucket | two-contact friction; settling; secondary contact | M | HomeInterior | Local HI ladder/bucket; wall/floor collision | wall/floor friction; ladder angle/mass; bucket distance |
| R053 | Falling weight tips seesaw and launches ball into cup | fall; pivot; launch; containment | H | Empty Play Room | Reuse seesaw; Local cup; Proc ball/weight | falling mass/height; lever ratio; ball mass |
| R054 | Rolling tire glances off block and topples two cans | rolling; glancing impact; ordered topples | H | Warehouse composition | Meshy-first tire; Local cans/box | tire speed/spin; impact offset; can mass |
| R056 | Double pendulum lower bob strikes target | two joints; coupled motion; impact | H | Machine Shop 01 | Meshy apparatus shell; analytic links | initial angles; link lengths/masses; damping |
| R057 | Hinged door swings, pushes a light carton, and damps | hinge axis; contact; angular decay | E | HomeInterior | Catalog-first/Meshy door; Local WH carton | door angle; hinge damping; carton mass |
| R058 | Hinged gate is struck by a ball | contact-driven hinge rotation | M | Warehouse composition | Meshy-first gate; Proc ball | ball speed; gate mass; hinge damping |
| R060 | Low- and high-damping mass–spring carts oscillate side by side | matched starts; damping-envelope contrast | E | Machine Shop 02 | Two Local springs; reusable cart/track pair | damping pair; stiffness; cart masses; initial extension |
| R061 | Compressed spring launches a cart into a lightweight target | stored energy; release; detachment; target contact | M | Empty Workshop | Existing spring; reusable cart; Local target box | spring stiffness/compression; cart mass; track friction |
| R063 | Bungee ball drops and rebounds | slack fall; unilateral stretch; damping | H | Wrestling Gym | Existing Pilot assets/Hessian cord | rest length; stiffness; damping; payload mass |
| R066 | Turntable accelerates until two props slip at different times | angular drive; friction limits; ordered slips | H | HomeInterior | Meshy turntable; Local cup/book | angular acceleration; radial placement; friction pair |
| R067 | Tether breaks and the released load lands on a pallet | break threshold; post-break fall; impact | M | Hangar Interior | Local cable/chain, load and WH pallet | break threshold; load mass; tether length; pallet height |
| R068 | Pendulum hits an angular stop | limited axis; stop rebound | M | HomeInterior | Reuse pendulum apparatus | stop angle; release angle; stop restitution/damping |
| R069 | Two-link hinged chain swings and strikes a hanging target | multiple joint frames; terminal contact | M | Warehouse composition | Local WH chain/target dressing; analytic links | link lengths/masses; release angles; target position |
| R070 | Spring-loaded hinged flap returns after releasing a small block | angular drive; release; return damping | M | Machine Shop 01 | Meshy/Proc flap; Local block | angular stiffness/damping; flap angle; block mass |
| R071 | Steady wind pushes a light ball past two floor markers | audited aerodynamic drift and direction | E | Hangar Interior | Local/Proc light ball; Local markers | wind speed; drag coefficient; ball mass |
| R072 | Wind pushes light and heavy balls toward separate cones | shared wind; mass/drag contrast | E | Hangar Interior | Reuse ball pair; Local/Proc cones | wind speed; mass pair; drag pair |
| R073 | Centered ball impact translates a box into a stop block | impulse; bounded rotation; terminal contact | E | HomeInterior | Local boxes; Proc ball | ball speed; box mass; friction; stop distance |
| R074 | Off-center impact spins a box through two floor markers | contact torque and angular response | M | Empty Workshop | Reuse R073 assets; Local markers | impact offset/speed; box inertia; marker angle |
| R075 | Spinning rod follows a ballistic arc | COM trajectory plus rigid rotation | M | Hangar Interior | Proc rod with realistic material | linear speed; angular speed; rod length |
| R076 | Steel ball hits glass below fracture threshold | contact without fracture | E | Ballroom | Qualified reusable glass Geometry Collection | impact energy; fracture threshold; glass thickness |
| R077 | Steel ball shatters glass above threshold | contact-before-fracture; fragments | H | Ballroom | Reuse R076 glass/ball | impact energy; fracture threshold; fragment scale |
| R078 | Steel ball breaks a wooden board supporting two cans | threshold fracture; support loss; fragments | H | Warehouse composition | Qualified Oak board Geometry Collection; Local cans | impact energy; board thickness; can masses |
| R079 | Loaded wooden crate fractures and releases three balls | contact-causal break; contents spill; settling | H | Warehouse composition | Qualified Wooden Crate 02 Geometry Collection; Proc balls | drop height; crate strength; content count/mass |
| R081 | Steel ball curves toward a side magnet before target contact | radial trace; curved path; final hit | H | Machine Shop 02 | Meshy-first magnet + steel-ball set | field strength/softening; source offset; ball speed |
| R082 | Magnet pulls a steel puck across static friction | radial trace; hold-to-slide transition | M | Bank Vault | Reuse magnet; Proc/Local steel puck | field strength; source distance; static friction; puck mass |
| R083 | Two suspended steel bobs move toward a central magnet | two hinges; two radial traces; bob contact | H | Bank Vault | Reuse magnet; Meshy/Proc frame/bobs | field strength; pendulum length; bob spacing/mass |
| R084 | Magnetic pendulum is deflected between two magnets | hinge motion; competing fields; final basin | H | Machine Shop 02 | Reuse magnet kit and pendulum | field-strength pair; magnet spacing; release angle |
| R085 | Twelve phase-shifted spring sliders move along fixed star-shaped diameter guides and form an internally rolling circle | signed guide motion; common lower spring anchor; staggered center crossings; phase geometry | H | Machine Shop 01 | Meshy/Proc star guide, twelve sliders/springs and lower anchor | spring stiffness; slider mass; oscillation amplitude; phase offset |
| R086 | A hanging ball pulls a whirling tabletop ball inward through a central eyelet | compliant constant rope path; shrinking radius; angular-speed increase | H | University Workshop | Four-slab Oak table with real 6 cm opening; metal eyelet; two balls and runtime cord | hanging mass; tangential speed; rope length; table friction |

Supplementary candidate R087 (not counted in Core80): a driven hub rotates six radial guides while displaced spring sliders damp toward one common radius. It reuses the same generic angular and linear drives but is physically distinct from the fixed-guide phase geometry of R085.

### Cloth with rigid participants (12)

| ID | Case | Physics evidence | D | Physics cap. | Scene | Participating assets | Adjustable variables |
|---|---|---|:---:|:---:|---|---|---|
| C001 | Flag fixed to a pole and weighted base in steady wind | fixed edge; mean wind displacement | E | Ready | Country Club | Cloth, pole, base and ground | wind speed; cloth stiffness/mass; drag coefficient |
| C002 | Gusting flag repeatedly brushes a spherical pole finial | pin preservation; periodic billow; sphere contact | M | Ready | Country Club | Cloth, pole/base, rigid finial and ground | gust amplitude/frequency; finial position/radius; stiffness |
| C003 | Sail panel fixed to a mast beside a rigid boom | fixed edge; oblique wind response | E | Ready | Hangar Interior | Cloth, mast, boom and deck block | wind direction/speed; sail aspect ratio; stiffness |
| C004 | Silk scarf drops over an exercise ball held by a floor ring | sphere contacts; settling | E | Ready | Dancing Hall | Cloth, ball, support ring and floor | drop height; ball radius; cloth friction/stiffness |
| C005 | Terry towel drapes over a round ottoman and reaches the floor | sphere/floor contacts; final span | E | Ready | HomeInterior | Cloth, HI puff/ottoman and floor | cloth size/mass; ottoman radius; friction |
| C006 | Tablecloth falls onto a round café table with cup and plate | multi-collider contact; stable drape | E | Ready (G1) | Ballroom | Cloth, table, cup, plate and floor | drop height; table radius; prop layout; cloth stiffness |
| C007 | Warehouse tarp falls across stacked crates and a steel beam | multiple box contacts; settling | H | Ready (G1) | Empty Warehouse | Cloth, three WH crates, beam and floor | crate layout; cloth size/mass; contact friction |
| C008 | Side-pinned curtain is brushed by a translating partition | kinematic box contact; recovery | H | Ready (G1) | Large Corridor | Cloth, sliding partition, rail and floor | partition speed/stroke; cloth stiffness/damping; gap |
| C009 | Blanket falls over a slowly translating exercise ball | kinematic sphere contact; asymmetric drape | M | Ready (G1) | Climbing Gym | Cloth, moving ball, guide rail and floor | ball speed/trajectory; radius; cloth friction |
| C010 | Sheet rides on a translating tray, loses support, and lands | moving support; support loss; floor settling | M | Ready (G1) | University Workshop | Cloth, tray, two bumpers and floor | tray acceleration/stroke; tray friction; cloth mass |
| C011 | Canvas cover over a barrel lifts in gusts and catches on a crate | cylinder/box contacts; wind lift | H | Ready (G1) | Warehouse composition | Cloth, WH barrel, crate, pallet and floor | wind speed/gust; barrel radius; crate gap; cloth stiffness |
| C012 | A moving horseshoe magnet pulls the metal base from a three-body stack under a cloth | radial-force collapse; rigid trajectory handoff; cloth slide to tabletop | H | Ready (G2) | University Workshop | Cloth, magnet, metal/wood/ceramic stack, table and floor | field strength; magnet approach speed; stack friction; cloth stiffness |

## Asset requirements

### Existing first

- HomeInterior: 102 meshes, 100 collision-ready; furniture, books, kitchen props, ladder, puff/ottoman and prepared room.
- warehouse_low: 57 collision-ready meshes; barrels, crates, boxes, pallets, workbench, beam, spool, cable/chain and industrial dressing.
- Qualified bowling pack, Oak Wood Planks, Hessian 230, scratched phosphate, procedural physics bodies, and 30 local Poly Haven interior HDRIs.
- No new broad environment download is needed. Warehouse compositions use warehouse_low props over an open workshop/warehouse HDRI.

### Catalog-first Meshy batch

After searching the existing Catalog, expect roughly **18–24 reusable hero assets**, not one new asset per case:

1. Open basket/hoop and open funnel.
2. Wrecking-ball frame, billiards table/cue, Newton-cradle frame.
3. Reusable cart, seesaw, turntable, double-pendulum frame, spring-cart dressing.
4. Spinning top, coin, tire, door, gate, flag/banner stand and sail mast.
5. Bar/horseshoe magnet, reusable steel-target set, and the C012 metal/wood/ceramic stack props.
6. Glass pane, wooden board, and crate source meshes for UE Geometry Collection preparation.
7. Cloth-rigid presentation gaps only after Local search: round café table, sliding partition, guide rail, tray bumper, radial slider carousel, and holed tabletop/eyelet. Most other participants reuse HomeInterior/warehouse_low or simple materialized geometry.

The open funnel and basket/hoop use qualified or visually accepted Meshy shells with analytic open collision. The glass, board, and crate now use qualified reusable Geometry Collections. Meshy generation alone is never qualification: open assets must preserve a genuinely open collision passage, and destruction sources must survive UE Geometry Collection preparation. Fab/manual download remains only a fallback after those checks fail.

### HDRI composition rule

For all Poly Haven HDRI scenes, enlarge the HDRIBackdrop projection, place the experiment near its central clear region, avoid panorama seams and curved side walls, and prefer a longer focal length/lower FOV. If the geometry still reads distorted, switch to another local HDRI rather than forcing camera alignment.

## Remaining work after approval

1. Update the CSV registry once, preserving retained IDs and their nine image-text assignments.
2. Search/qualify Local assets, then acquire only the remaining Meshy batch.
3. Qualify HDRI Maps on demand rather than all 30 up front.
4. Prepare reusable glass/wood Geometry Collections.
5. Use the qualified `G1` path for C006–C011 and the qualified one-way `G2` trajectory handoff for C012; formal cases still require their actual assets, CaseSpecs, and visual review.
6. Use the qualified R085 compound constraints and R086 fixed-waypoint path-length spring when authoring their formal cases; the latter is explicitly a stiff unilateral compliant approximation rather than an exact inextensible rope.
7. Use independent ordered formal directories such as `001_R001_ball_steps` through `080_C012_magnetic_stack_cloth`; exact execution order is frozen after roster approval.
