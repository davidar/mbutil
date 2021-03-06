#!/usr/bin/env python

# MBUtil: a tool for MBTiles files
# Supports importing, exporting, and more
#
# (c) Development Seed 2012
# Licensed under BSD

# for additional reference on schema see:
# https://github.com/mapbox/node-mbtiles/blob/master/lib/schema.sql

import sqlite3, uuid, sys, logging, time, os, json, zlib, re, gzip, io, zipfile, tarfile, StringIO

logger = logging.getLogger(__name__)

def flip_y(zoom, y):
    return (2**zoom-1) - y

def mbtiles_setup(cur):
    cur.execute("""
        create table tiles (
            zoom_level integer,
            tile_column integer,
            tile_row integer,
            tile_data blob);
            """)
    cur.execute("""create table metadata
        (name text, value text);""")
    cur.execute("""CREATE TABLE grids (zoom_level integer, tile_column integer,
    tile_row integer, grid blob);""")
    cur.execute("""CREATE TABLE grid_data (zoom_level integer, tile_column
    integer, tile_row integer, key_name text, key_json text);""")
    cur.execute("""create unique index name on metadata (name);""")
    cur.execute("""create unique index tile_index on tiles
        (zoom_level, tile_column, tile_row);""")

def mbtiles_connect(mbtiles_file):
    try:
        con = sqlite3.connect(mbtiles_file)
        return con
    except Exception as e:
        logger.error("Could not connect to database")
        logger.exception(e)
        sys.exit(1)

def optimize_connection(cur):
    cur.execute("""PRAGMA synchronous=0""")
    cur.execute("""PRAGMA locking_mode=EXCLUSIVE""")
    cur.execute("""PRAGMA journal_mode=DELETE""")

def compression_prepare(cur, con):
    cur.execute("""
      CREATE TABLE if not exists images (
        tile_data blob,
        tile_id VARCHAR(256));
    """)
    cur.execute("""
      CREATE TABLE if not exists map (
        zoom_level integer,
        tile_column integer,
        tile_row integer,
        tile_id VARCHAR(256));
    """)

def optimize_database(cur):
    logger.debug('analyzing db')
    cur.execute("""ANALYZE;""")
    logger.debug('cleaning db')
    cur.execute("""VACUUM;""")

def compression_do(cur, con, chunk):
    overlapping = 0
    unique = 0
    total = 0
    cur.execute("select count(zoom_level) from tiles")
    res = cur.fetchone()
    total_tiles = res[0]
    logging.debug("%d total tiles to fetch" % total_tiles)
    for i in range(total_tiles // chunk + 1):
        logging.debug("%d / %d rounds done" % (i, (total_tiles / chunk)))
        ids = []
        files = []
        start = time.time()
        cur.execute("""select zoom_level, tile_column, tile_row, tile_data
            from tiles where rowid > ? and rowid <= ?""", ((i * chunk), ((i + 1) * chunk)))
        logger.debug("select: %s" % (time.time() - start))
        rows = cur.fetchall()
        for r in rows:
            total = total + 1
            if r[3] in files:
                overlapping = overlapping + 1
                start = time.time()
                query = """insert into map
                    (zoom_level, tile_column, tile_row, tile_id)
                    values (?, ?, ?, ?)"""
                logger.debug("insert: %s" % (time.time() - start))
                cur.execute(query, (r[0], r[1], r[2], ids[files.index(r[3])]))
            else:
                unique = unique + 1
                id = str(uuid.uuid4())

                ids.append(id)
                files.append(r[3])

                start = time.time()
                query = """insert into images
                    (tile_id, tile_data)
                    values (?, ?)"""
                cur.execute(query, (str(id), sqlite3.Binary(r[3])))
                logger.debug("insert into images: %s" % (time.time() - start))
                start = time.time()
                query = """insert into map
                    (zoom_level, tile_column, tile_row, tile_id)
                    values (?, ?, ?, ?)"""
                cur.execute(query, (r[0], r[1], r[2], id))
                logger.debug("insert into map: %s" % (time.time() - start))
        con.commit()

def compression_finalize(cur):
    cur.execute("""drop table tiles;""")
    cur.execute("""create view tiles as
        select map.zoom_level as zoom_level,
        map.tile_column as tile_column,
        map.tile_row as tile_row,
        images.tile_data as tile_data FROM
        map JOIN images on images.tile_id = map.tile_id;""")
    cur.execute("""
          CREATE UNIQUE INDEX map_index on map
            (zoom_level, tile_column, tile_row);""")
    cur.execute("""
          CREATE UNIQUE INDEX images_id on images
            (tile_id);""")
    cur.execute("""vacuum;""")
    cur.execute("""analyze;""")

def getDirs(path):
    return [name for name in os.listdir(path)
        if os.path.isdir(os.path.join(path, name))]

def disk_to_mbtiles(directory_path, mbtiles_file, **kwargs):
    logger.info("Importing disk to MBTiles")
    logger.debug("%s --> %s" % (directory_path, mbtiles_file))
    con = mbtiles_connect(mbtiles_file)
    cur = con.cursor()
    optimize_connection(cur)
    mbtiles_setup(cur)
    #~ image_format = 'png'
    image_format = kwargs.get('format', 'png')
    try:
        metadata = json.load(open(os.path.join(directory_path, 'metadata.json'), 'r'))
        image_format = kwargs.get('format')
        for name, value in metadata.items():
            cur.execute('insert into metadata (name, value) values (?, ?)',
                (name, value))
        logger.info('metadata from metadata.json restored')
    except IOError:
        logger.warning('metadata.json not found')

    count = 0
    start_time = time.time()
    msg = ""

    for zoomDir in getDirs(directory_path):
        if kwargs.get("scheme") == 'ags':
            if not "L" in zoomDir:
                logger.warning("You appear to be using an ags scheme on an non-arcgis Server cache.")
            z = int(zoomDir.replace("L", ""))
        else:
            if "L" in zoomDir:
                logger.warning("You appear to be using a %s scheme on an arcgis Server cache. Try using --scheme=ags instead" % kwargs.get("scheme"))
            z = int(zoomDir)
        for rowDir in getDirs(os.path.join(directory_path, zoomDir)):
            if kwargs.get("scheme") == 'ags':
                y = flip_y(z, int(rowDir.replace("R", ""), 16))
            else:
                x = int(rowDir)
            for current_file in os.listdir(os.path.join(directory_path, zoomDir, rowDir)):
                file_name, ext = current_file.split('.',1)
                f = open(os.path.join(directory_path, zoomDir, rowDir, current_file), 'rb')
                file_content = f.read()
                f.close()
                if kwargs.get('scheme') == 'xyz':
                    y = flip_y(int(z), int(file_name))
                elif kwargs.get("scheme") == 'ags':
                    x = int(file_name.replace("C", ""), 16)
                else:
                    y = int(file_name)

                if (ext == image_format):
                    logger.debug(' Read tile from Zoom (z): %i\tCol (x): %i\tRow (y): %i' % (z, x, y))
                    cur.execute("""insert into tiles (zoom_level,
                        tile_column, tile_row, tile_data) values
                        (?, ?, ?, ?);""",
                        (z, x, y, sqlite3.Binary(file_content)))
                    count = count + 1
                    if (count % 100) == 0:
                        for c in msg: sys.stdout.write(chr(8))
                        msg = "%s tiles inserted (%d tiles/sec)" % (count, count / (time.time() - start_time))
                        sys.stdout.write(msg)
                elif (ext == 'grid.json'):
                    logger.debug(' Read grid from Zoom (z): %i\tCol (x): %i\tRow (y): %i' % (z, x, y))
                    # Remove potential callback with regex
                    file_content = file_content.decode('utf-8')
                    has_callback = re.match(r'[\w\s=+-/]+\(({(.|\n)*})\);?', file_content)
                    if has_callback:
                        file_content = has_callback.group(1)
                    utfgrid = json.loads(file_content)

                    data = utfgrid.pop('data')
                    compressed = zlib.compress(json.dumps(utfgrid).encode())
                    cur.execute("""insert into grids (zoom_level, tile_column, tile_row, grid) values (?, ?, ?, ?) """, (z, x, y, sqlite3.Binary(compressed)))
                    grid_keys = [k for k in utfgrid['keys'] if k != ""]
                    for key_name in grid_keys:
                        key_json = data[key_name]
                        cur.execute("""insert into grid_data (zoom_level, tile_column, tile_row, key_name, key_json) values (?, ?, ?, ?, ?);""", (z, x, y, key_name, json.dumps(key_json)))

    logger.debug('tiles (and grids) inserted.')
    optimize_database(con)

def get_metadata(con):
    d = {}
    for k,v in con.execute('select name, value from metadata;').fetchall():
        if k == 'json':
            d.update(json.loads(v))
        else:
            d[k] = v
    return d

def mbtiles_metadata_to_disk(mbtiles_file, **kwargs):
    logger.debug("Exporting MBTiles metatdata from %s" % (mbtiles_file))
    con = mbtiles_connect(mbtiles_file)
    metadata = get_metadata(con)
    print(json.dumps(metadata, indent=2))

def write_file(base_path, file_path, file_name, file_data):
    if type(base_path) is zipfile.ZipFile:
        base_path.writestr(os.path.join(file_path, file_name), file_data)
    elif type(base_path) is tarfile.TarFile:
        tarinfo = tarfile.TarInfo(os.path.join(file_path, file_name))
        tarinfo.size = len(file_data)
        base_path.addfile(tarinfo, StringIO.StringIO(file_data))
        base_path.members = [] # fix memory leak
    else:
        if not os.path.isdir(os.path.join(base_path, file_path)):
            os.makedirs(os.path.join(base_path, file_path))
        f = open(os.path.join(base_path, file_path, file_name), 'wb')
        f.write(file_data)
        f.close()

def mbtiles_to_disk(mbtiles_file, base_path, **kwargs):
    logger.debug("Exporting MBTiles to disk")
    logger.debug("%s --> %s" % (mbtiles_file, base_path))
    if base_path.endswith('.zip'):
        base_path = zipfile.ZipFile(base_path, 'w', zipfile.ZIP_DEFLATED, True)
    elif base_path.endswith('.tar'):
        base_path = tarfile.open(base_path, 'w')
    elif base_path.endswith('.tar.gz') or base_path.endswith('.tgz'):
        base_path = tarfile.open(base_path, 'w:gz')
    elif base_path.endswith('.tar.bz2'):
        base_path = tarfile.open(base_path, 'w:bz2')
    con = mbtiles_connect(mbtiles_file)
    metadata = get_metadata(con)
    write_file(base_path, '', 'metadata.json', json.dumps(metadata, indent=4))
    tile_format = kwargs.get('format', 'png')
    if 'format' in metadata:
        tile_format = metadata['format']
    count = con.execute('select count(zoom_level) from tiles;').fetchone()[0]
    done = 0

    # if interactivity
    formatter = metadata.get('formatter')
    if formatter:
        formatter_json = {"formatter":formatter}
        write_file(base_path, '', 'layer.json', json.dumps(formatter_json))

    tiles = con.execute('select zoom_level, tile_column, tile_row, tile_data from tiles;')
    t = tiles.fetchone()
    while t:
        z = t[0]
        x = t[1]
        y = t[2]
        tile_data = t[3]
        if kwargs.get('scheme') == 'xyz':
            y = flip_y(z,y)
            tile_dir = os.path.join(str(z), str(x))
        elif kwargs.get('scheme') == 'wms':
            tile_dir = os.path.join(
                "%02d" % (z),
                "%03d" % (int(x) / 1000000),
                "%03d" % ((int(x) / 1000) % 1000),
                "%03d" % (int(x) % 1000),
                "%03d" % (int(y) / 1000000),
                "%03d" % ((int(y) / 1000) % 1000))
        else:
            tile_dir = os.path.join(str(z), str(x))
        if kwargs.get('scheme') == 'wms':
            tile = '%03d.%s' % (int(y) % 1000, tile_format)
        else:
            tile = '%s.%s' % (y, tile_format)
        if len(tile_data) > 0:
            if tile_format == 'pbf':
                tile_data = gzip.GzipFile(fileobj=io.BytesIO(tile_data), mode='rb').read()
            write_file(base_path, tile_dir, tile, tile_data)
            done += 1
        else:
            count -= 1
        logger.info('%s / %s tiles exported' % (done, count))
        t = tiles.fetchone()

    # grids
    callback = kwargs.get('callback')
    done = 0
    try:
        count = con.execute('select count(zoom_level) from grids;').fetchone()[0]
        grids = con.execute('select zoom_level, tile_column, tile_row, grid from grids;')
        g = grids.fetchone()
    except sqlite3.OperationalError:
        g = None # no grids table
    while g:
        zoom_level = g[0] # z
        tile_column = g[1] # x
        y = g[2] # y
        grid_data_cursor = con.execute('''select key_name, key_json FROM
            grid_data WHERE
            zoom_level = %(zoom_level)d and
            tile_column = %(tile_column)d and
            tile_row = %(y)d;''' % locals() )
        if kwargs.get('scheme') == 'xyz':
            y = flip_y(zoom_level,y)
        grid_dir = os.path.join(str(zoom_level), str(tile_column))
        grid = '%s.grid.json' % (y)
        grid_json = json.loads(zlib.decompress(g[3]).decode('utf-8'))
        # join up with the grid 'data' which is in pieces when stored in mbtiles file
        grid_data = grid_data_cursor.fetchone()
        data = {}
        while grid_data:
            data[grid_data[0]] = json.loads(grid_data[1])
            grid_data = grid_data_cursor.fetchone()
        grid_json['data'] = data
        if callback in (None, "", "false", "null"):
            write_file(base_path, grid_dir, grid, json.dumps(grid_json))
        else:
            write_file(base_path, grid_dir, grid, '%s(%s);' % (callback, json.dumps(grid_json)))
        done = done + 1
        logger.info('%s / %s grids exported' % (done, count))
        g = grids.fetchone()
