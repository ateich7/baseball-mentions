# (network_name, year) -> (play_by_play, color_analyst)
# Network names must match exactly as they appear in tv_home / tv_national columns.
CREW = {

    # ── YES – New York Yankees ────────────────────────────────────────────────
    ('YES', 2021): ('Michael Kay', 'Paul O\'Neill'),
    ('YES', 2023): ('Michael Kay', 'David Cone'),
    ('YES', 2024): ('Michael Kay', 'David Cone'),
    ('YES', 2025): ('Michael Kay', 'David Cone'),
    ('YES', 2026): ('Michael Kay', 'David Cone'),

    # ── NESN – Boston Red Sox ─────────────────────────────────────────────────
    ('NESN', 2021): ('Dave O\'Brien', 'Jerry Remy'),       # Remy retired/passed away Oct 2021
    ('NESN', 2025): ('Dave O\'Brien', 'Lou Merloni'),
    ('NESN', 2026): ('Dave O\'Brien', 'Lou Merloni'),
    ('NESN+', 2026): ('Dave O\'Brien', 'Lou Merloni'),     # overflow feed

    # ── Angels (Bally Sports West → FanDuel Sports Network West) ─────────────
    ('Bally Sports West', 2024): ('Wayne Randazzo', 'Mark Gubicza'),
    ('FanDuel Sports Network West', 2024): ('Wayne Randazzo', 'Mark Gubicza'),
    ('FanDuel Sports Network West', 2025): ('Wayne Randazzo', 'Mark Gubicza'),
    ('FanDuel Sports Network West', 2026): ('Wayne Randazzo', 'Mark Gubicza'),

    # ── SportsNet LA – Los Angeles Dodgers ────────────────────────────────────
    ('SportsNet LA', 2020): ('Joe Davis', 'Orel Hershiser'),
    ('SportsNet LA', 2021): ('Joe Davis', 'Orel Hershiser'),
    ('SportsNet LA', 2024): ('Joe Davis', 'Orel Hershiser'),
    ('SportsNet LA', 2025): ('Joe Davis', 'Orel Hershiser'),
    ('SportsNet LA', 2026): ('Joe Davis', 'Orel Hershiser'),

    # ── Rays (Bally Sports Sun → FanDuel Sports Network Sun → Rays.TV) ───────
    ('Bally Sports Sun', 2024): ('Dewayne Staats', 'Brian Anderson'),
    ('FanDuel Sports Network Sun', 2025): ('Dewayne Staats', 'Brian Anderson'),
    ('Rays.TV', 2026): ('Dewayne Staats', 'Brian Anderson'),

    # ── MASN – Baltimore Orioles ──────────────────────────────────────────────
    ('MASN', 2024): ('Kevin Brown', 'Ben McDonald'),
    ('MASN', 2025): ('Kevin Brown', 'Ben McDonald'),
    ('MASN 2', 2025): ('Kevin Brown', 'Ben McDonald'),     # overflow channel

    # ── Space City Home Network – Houston Astros ──────────────────────────────
    ('Space City Home Network', 2023): ('Todd Kalas', 'Geoff Blum'),
    ('Space City Home Network', 2025): ('Todd Kalas', 'Geoff Blum'),
    ('Space City Home Network (Sp)', 2025): ('Todd Kalas', 'Geoff Blum'),
    ('Space City Home Network', 2026): ('Todd Kalas', 'Geoff Blum'),

    # ── Mariners (Root Sports Northwest → Mariners.TV) ────────────────────────
    ('ROOTNW', 2024): ('Aaron Goldsmith', 'Ryan Rowland-Smith'),
    ('ROOTNW', 2025): ('Aaron Goldsmith', 'Ryan Rowland-Smith'),
    ('Mariners.TV', 2026): ('Aaron Goldsmith', 'Angie Mentink'),
    ('SEA Video', 2026): ('Aaron Goldsmith', 'Angie Mentink'),

    # ── Sportsnet – Toronto Blue Jays ─────────────────────────────────────────
    ('Sportsnet', 2021): ('Dan Shulman', 'Buck Martinez'),
    ('Sportsnet', 2024): ('Dan Shulman', 'Buck Martinez'),
    ('Sportsnet', 2025): ('Dan Shulman', 'Buck Martinez'),
    ('SN1', 2025): ('Dan Shulman', 'Buck Martinez'),       # overflow channel

    # ── NBCSCA – Oakland / Sacramento Athletics ───────────────────────────────
    # Glen Kuiper fired May 2023; Jenny Cavnar named primary PBP for 2024
    ('NBCSCA', 2023): ('Glen Kuiper', 'Dallas Braden'),
    ('NBCSCA', 2024): ('Jenny Cavnar', 'Dallas Braden'),
    ('NBCSCA', 2025): ('Jenny Cavnar', 'Dallas Braden'),
    ('NBCSCA', 2026): ('Jenny Cavnar', 'Dallas Braden'),
    ('NBCS BA', 2024): ('Duane Kuiper', 'Mike Krukow'),    # Giants, not Athletics
    ('NBCS BA', 2025): ('Duane Kuiper', 'Mike Krukow'),
    ('NBCS BA', 2026): ('Duane Kuiper', 'Mike Krukow'),

    # ── Guardians.TV – Cleveland Guardians ────────────────────────────────────
    ('Guardians.TV Presented by Progressive', 2025): ('Matt Underwood', 'Rick Manning'),
    ('Guardians.TV Presented by Progressive', 2026): ('Matt Underwood', 'Rick Manning'),

    # ── Tigers (Bally Sports Detroit → FanDuel Sports Network Detroit) ────────
    ('Bally Sports Detroit', 2024): ('Jason Benetti', 'Andy Dirks'),
    ('FanDuel Sports Network Detroit', 2024): ('Jason Benetti', 'Andy Dirks'),
    ('FanDuel Sports Network Detroit', 2025): ('Jason Benetti', 'Andy Dirks'),
    ('FanDuel Sports Network Detroit Extra', 2025): ('Jason Benetti', 'Andy Dirks'),
    ('Detroit SportsNet', 2026): ('Jason Benetti', 'Andy Dirks'),

    # ── Marlins (Bally Sports Florida → FanDuel → Marlins.TV) ────────────────
    ('Bally Sports Florida', 2024): ('Paul Severino', 'Tommy Hutton'),
    ('FanDuel Sports Network Florida', 2025): ('Kyle Sielaff', 'Tommy Hutton'),
    ('Marlins.TV presented by Werner, Hoffman, Greig & Garcia', 2026): ('Kyle Sielaff', 'Tommy Hutton'),

    # ── Marquee Sports Network – Chicago Cubs ─────────────────────────────────
    ('Marquee Sports Network', 2021): ('Jon Sciambi', 'Jim Deshaies'),
    ('Marquee Sports Network', 2023): ('Jon Sciambi', 'Jim Deshaies'),
    ('Marquee Sports Network', 2024): ('Jon Sciambi', 'Jim Deshaies'),
    ('Marquee Sports Network', 2025): ('Jon Sciambi', 'Jim Deshaies'),
    ('Marquee Sports Network', 2026): ('Jon Sciambi', 'Jim Deshaies'),

    # ── Chicago Sports Network (CHSN) – Chicago White Sox ────────────────────
    ('Chicago Sports Network', 2025): ('John Schriffen', 'Steve Stone'),
    ('Chicago Sports Network', 2026): ('John Schriffen', 'Steve Stone'),
    ('CHSN+', 2025): ('John Schriffen', 'Steve Stone'),

    # ── NBC Sports Philadelphia – Philadelphia Phillies ───────────────────────
    ('NBCSP', 2021): ('Tom McCarthy', 'John Kruk'),
    ('NBCSP', 2025): ('Tom McCarthy', 'John Kruk'),
    ('NBCSP+', 2025): ('Tom McCarthy', 'John Kruk'),

    # ── SNY – New York Mets ───────────────────────────────────────────────────
    ('SNY', 2025): ('Gary Cohen', 'Ron Darling'),

    # ── SportsNet Pittsburgh – Pittsburgh Pirates ─────────────────────────────
    ('SportsNet Pittsburgh', 2025): ('Greg Brown', 'Bob Walk'),
    ('SportsNet Pittsburgh', 2026): ('Greg Brown', 'Bob Walk'),
    ('SportsNet Pittsburgh ', 2026): ('Greg Brown', 'Bob Walk'),  # trailing-space variant in data

    # ── Cardinals (Bally Sports Midwest → FanDuel Sports Network Midwest) ─────
    ('Bally Sports Midwest', 2023): ('Chip Caray', 'Jim Edmonds'),
    ('FanDuel Sports Network Midwest', 2025): ('Chip Caray', 'Ricky Horton'),
    ('FanDuel Sports Network Midwest Extra', 2025): ('Chip Caray', 'Ricky Horton'),

    # ── Reds (FanDuel Sports Network Ohio → Reds.TV) ─────────────────────────
    ('Bally Sports Ohio', 2024): ('John Sadak', 'Barry Larkin'),
    ('FanDuel Sports Network Ohio', 2025): ('John Sadak', 'Barry Larkin'),
    ('Reds.TV', 2026): ('John Sadak', 'Barry Larkin'),

    # ── Royals (Bally / FanDuel → Royals.TV) ─────────────────────────────────
    ('Bally Sports Kansas City', 2024): ('Ryan Lefebvre', 'Rex Hudler'),
    ('FanDuel Sports Network Kansas City', 2025): ('Ryan Lefebvre', 'Rex Hudler'),
    ('Royals.TV', 2026): ('Ryan Lefebvre', 'Rex Hudler'),

    # ── Twins (Bally Sports North → Twins.TV) ────────────────────────────────
    ('Bally Sports North', 2024): ('Cory Provus', 'Justin Morneau'),
    ('Twins.TV Presented by Progressive', 2025): ('Cory Provus', 'Justin Morneau'),
    ('Twins.TV Presented by Progressive', 2026): ('Cory Provus', 'Justin Morneau'),

    # ── Brewers (Bally Sports Wisconsin → FanDuel Sports Network Wisconsin) ───
    ('Bally Sports Wisconsin', 2024): ('Jeff Levering', 'Bill Schroeder'),
    ('FanDuel Sports Network Wisconsin', 2025): ('Jeff Levering', 'Bill Schroeder'),

    # ── Braves (Bally Sports South/Southeast → FanDuel Sports Network South) ──
    ('Bally Sports South', 2023): ('Brandon Gaudin', 'Jeff Francoeur'),
    ('Bally Sports South', 2024): ('Brandon Gaudin', 'C.J. Nitkowski'),
    ('Bally Sports Southeast', 2024): ('Brandon Gaudin', 'C.J. Nitkowski'),
    ('FanDuel Sports Network South', 2025): ('Brandon Gaudin', 'C.J. Nitkowski'),

    # ── Nationals.TV – Washington Nationals ───────────────────────────────────
    ('Nationals.TV', 2026): ('Dan Kolko', 'Kevin Frandsen'),

    # ── Rangers Sports Network / Bally Sports Southwest – Texas Rangers ───────
    ('Bally Sports Southwest', 2021): ('Dave Raymond', 'C.J. Nitkowski'),
    ('Bally Sports Southwest', 2023): ('Dave Raymond', 'C.J. Nitkowski'),
    ('Bally Sports Southwest', 2024): ('Dave Raymond', 'David Murphy'),
    ('Rangers Sports Network', 2025): ('Dave Raymond', 'Mike Bacsik'),
    ('Rangers Sports Network', 2026): ('Dave Raymond', 'Mike Bacsik'),
    ('CW33', 2025): ('Dave Raymond', 'Mike Bacsik'),       # OTA overflow feed

    # ── Dbacks.TV – Arizona Diamondbacks ─────────────────────────────────────
    ('Dbacks.TV', 2024): ('Steve Berthiaume', 'Bob Brenly'),
    ('Dbacks.TV', 2025): ('Steve Berthiaume', 'Bob Brenly'),
    ('Dbacks.TV', 2026): ('Steve Berthiaume', 'Bob Brenly'),

    # ── Rockies.TV – Colorado Rockies ────────────────────────────────────────
    ('Rockies.TV', 2024): ('Drew Goodman', 'Jeff Huson'),
    ('Rockies.TV', 2025): ('Drew Goodman', 'Jeff Huson'),
    ('Rockies.TV', 2026): ('Drew Goodman', 'Jeff Huson'),

    # ── Padres.TV – San Diego Padres ─────────────────────────────────────────
    ('Bally Sports San Diego', 2021): ('Don Orsillo', 'Mark Grant'),
    ('Padres.TV Presented by UC San Diego Health', 2025): ('Don Orsillo', 'Mark Grant'),
    ('Padres.TV Presented by UC San Diego Health', 2026): ('Don Orsillo', 'Mark Grant'),

    # ── National networks ─────────────────────────────────────────────────────

    # FOX / FS1 – Saturday Game of the Week + playoffs
    ('FOX', 2024): ('Joe Davis', 'John Smoltz'),
    ('FOX', 2025): ('Joe Davis', 'John Smoltz'),
    ('FOX', 2026): ('Joe Davis', 'John Smoltz'),
    ('FOX|FS1', 2025): ('Joe Davis', 'John Smoltz'),
    ('FS1', 2024): ('Joe Davis', 'John Smoltz'),
    ('FS1', 2025): ('Joe Davis', 'John Smoltz'),
    ('FS1|MLBN', 2025): ('Joe Davis', 'John Smoltz'),

    # ESPN – Sunday Night Baseball
    ('ESPN/ESPN App', 2023): ('Karl Ravech', 'David Cone'),
    ('ESPN/ESPN App', 2024): ('Karl Ravech', 'David Cone'),
    ('ESPN/ESPN App', 2025): ('Karl Ravech', 'David Cone'),
    ('ESPN/ESPN App|ESPN-INT', 2023): ('Karl Ravech', 'David Cone'),
    ('ABC/ESPN App', 2023): ('Karl Ravech', 'David Cone'),
    ('ABC/ESPN App', 2024): ('Karl Ravech', 'David Cone'),
    ('ABC/ESPN App', 2025): ('Karl Ravech', 'David Cone'),
    ('ABC/ESPN App|ABC-INT', 2023): ('Karl Ravech', 'David Cone'),
    # ESPN2 alternate KayRod Cast
    ('ESPN2', 2023): ('Michael Kay', 'Alex Rodriguez'),
    ('ESPN2', 2024): ('Michael Kay', 'Alex Rodriguez'),
    ('ESPN2|ESPN2-INT', 2023): ('Michael Kay', 'Alex Rodriguez'),

    # TBS / Max – Playoffs
    ('TBS', 2025): ('Brian Anderson', 'Ron Darling'),
    ('TBS|HBO Max', 2024): ('Brian Anderson', 'Ron Darling'),
    ('TBS|HBO Max', 2025): ('Brian Anderson', 'Ron Darling'),
    ('TBS|TruTV|HBO Max', 2023): ('Brian Anderson', 'Ron Darling'),
    ('TBS|TruTV|HBO Max', 2024): ('Brian Anderson', 'Ron Darling'),

    # Apple TV – Friday Night Baseball
    ('Apple TV', 2025): ('Wayne Randazzo', 'Dontrelle Willis'),

    # Amazon Prime Video – Yankees/local simulcast via YES (same crew as YES)
    ('Amazon Prime Video', 2025): ('Michael Kay', 'David Cone'),
    ('Amazon Prime Video', 2026): ('Michael Kay', 'David Cone'),

    # Roku – MLB Sunday Leadoff (uses home team's local crew; no dedicated booth)
    ('Roku', 2024): ('Gary Cohen', 'Ron Darling'),
    ('Roku', 2025): ('Gary Cohen', 'Ron Darling'),

    # Netflix – 2026 Opening Night
    ('Netflix', 2026): ('Matt Vasgersian', 'CC Sabathia'),

    # NBC / Peacock – Sunday Night Baseball 2026
    ('NBCSN / Peacock', 2026): ('Jason Benetti', 'Clayton Kershaw'),

    # MLB Network
    ('MLBN', 2025): ('Matt Vasgersian', 'Dan Plesac'),
    ('MLBN', 2024): ('Matt Vasgersian', 'Dan Plesac'),
}
