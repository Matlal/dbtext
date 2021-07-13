#!/usr/bin/python

"""
dbtext is a class to use for dynamically create test databases within SQL Server localdb


"""

# http://code.google.com/p/pyodbc/
import sqlite3

import pyodbc
import os, sys, subprocess, locale
import codecs
import shutil, struct
from string import Template
from glob import glob
from fnmatch import fnmatch
from datetime import datetime

class DBText:
    """
    This is an abstract class - use one of the subclasses specific to your database server.
    """
    connectionStringTemplate = None
    enforceVersion = None
    def __init__(self, database=None, master_connection=None):
        self.maxval = {}
        self.database_name = database
        self.iscreated = master_connection is not None
        self.isconnected = False
        self.startrv = ""
    
        # user master because attach detach
        try:
            self.cnxn = master_connection or self.make_connection("master")
            self.isconnected = True
        except pyodbc.Error as e:
            print("Unexpected error for db " + database + ":", e)
            raise
        
    def get_create_db_args(self, **kw):
        return ""
        
    def create(self, sqlfile=None, **kw):
        self.create_empty_db(**kw)
        self.populate_empty_db(sqlfile)

    def create_empty_db(self, **kw):
        try:
            attachsql = "CREATE DATABASE " + self.database_name + self.get_create_db_args(**kw) + ";"
            self.query(attachsql)
        except pyodbc.Error as e:
            print(f"Unexpected error for create db {self.database_name}:\n{attachsql}\n", e)
            raise

    def populate_empty_db(self, sqlfile):
        try:
            self.iscreated = True
            with self.make_connection(self.database_name) as ttcxn:
                if sqlfile and os.path.isfile(sqlfile):
                    self.read_sql_file(ttcxn, sqlfile)

                tables_dir_name = self.get_tables_dir_name()
                if os.path.isdir(tables_dir_name):
                    self.read_tables_dir(ttcxn, tables_dir_name)

                self.readrv(ttcxn)
        except pyodbc.Error as e:
            print(f"Unexpected error for populate empty db {self.database_name}:\n", e)
            raise

    def execute_setup_query(self, ttcxn, currQuery):
        try:
            ttcxn.cursor().execute(currQuery)
        except pyodbc.Error:
            print("Failed to execute query:\n" + repr(currQuery))
            raise
            
    def read_sql_file(self, ttcxn, sqlfile):
        currQuery = ''
        inComment = False
        with open(sqlfile) as f:
            for line in f:
                line = line.strip()
                if not line or "USE [" in line or line.startswith("--"):
                    continue
                if line.startswith("/*"):
                    inComment = True
                if inComment:
                    if line.endswith("*/"):
                        inComment = False
                    continue
               
                if line in [ "go", "GO" ]:
                    if currQuery:
                        self.execute_setup_query(ttcxn, currQuery)
                        currQuery = ''
                else:
                    if currQuery:
                        currQuery += "\n"
                    currQuery += line
        if currQuery.strip():
            self.execute_setup_query(ttcxn, currQuery)

    def read_tables_dir(self, ttcxn, tables_dir_name, verbose=False):
        failedFiles = []
        for tableFile in glob(os.path.join(tables_dir_name, "*.table")):
            try:
                if verbose:
                    print("Reading data from", tableFile)
                self.add_table_data(tableFile, ttcxn)
            except pyodbc.IntegrityError as ex:
                fk_constraint_string = "FOREIGN KEY constraint"
                if fk_constraint_string in ex.args[1]:
                    failedFiles.append(tableFile)
                else:
                    raise ex
                
        # Things often fail due to constraints, insert everything else and then try them again
        for tableFile in failedFiles:
            self.add_table_data(tableFile, ttcxn)
              
    @classmethod
    def expand_value(cls, value, *args):
        if "${" in value:
            return os.path.expandvars(value)
        elif "###NOWDATETIME###" in value:
            return value.replace('###NOWDATETIME###', datetime.now().strftime('%Y-%m-%dT%H:%M:%S'))
        else: 
            return value

    @classmethod      
    def parse_table_file(cls, fn):
        rows = []
        currRowData = []
        tablesDir = os.path.dirname(fn) 
        with open(fn) as f:
            for line in f:
                if line.startswith("ROW"):
                    if currRowData:
                        rows.append(currRowData)
                    currRowData = []
                elif ":" in line:
                    key, value = [ part.strip() for part in line.split(":", 1) ]
                    value = cls.expand_value(value, tablesDir, currRowData)    
                    currRowData.append((key, value))
        rows.append(currRowData)
        return rows
    
    @classmethod
    def write_table_file(self, rows, fn, asUpdate=False):
        with open(fn, 'w') as f:
            for il, row in enumerate(rows):
                row_id = "+" if asUpdate else str(il)
                header = "ROW:" + row_id + "\n"
                f.write(header)
                for colname, value in row:
                    rowStr = '   ' + colname + ": " + value + "\n"
                    f.write(rowStr)
                    
    @classmethod
    def package_blobs(cls, blobs, *args):
        return blobs[0]
        
    @classmethod                
    def make_blob(cls, blobFiles, blobType):
        blobs = [ open(fn, "rb").read() for fn in blobFiles ]
        blob_bytes = cls.package_blobs(blobs, blobType)
        return pyodbc.Binary(blob_bytes)

    def parse_blob(self, currRowDict, tablesDir):
        blobFileName, blobType = self.get_blob_file_name(currRowDict, self.get_blob_patterns())
        blobPath = os.path.join(tablesDir, blobFileName)
        if not os.path.isfile(blobPath):
            sys.stderr.write("ERROR: Could not find any blob files named " + blobFileName + "!\n")
            return pyodbc.Binary(b"")
        return self.make_blob([ blobPath ], blobType)
                    
    def parse_row_value(self, value, currRowDict, tablesDir):
        if value == "None":
            return None
        elif value == "<blob data>":
            return self.parse_blob(currRowDict, tablesDir)
        elif value.startswith("0x"): # hex string, convert to binary
            return pyodbc.Binary(struct.pack('<Q', int(value, 16)))
        else:
            return value
    
    def add_table_data(self, fn, ttcxn):
        tablesDir = os.path.dirname(fn)
        for currRowData in self.parse_table_file(fn):
            table_name = os.path.basename(fn)[:-6]
            currRowDict = {}
            for key, value in currRowData:
                currRowDict[key] = self.parse_row_value(value, currRowDict, tablesDir)
                
            self.insert_row(ttcxn, table_name, currRowDict)
          
    def insert_row(self, ttcxn, table_name, data, identity_insert=False):
        if not data:
            return
        
        valueStr = ("?," * len(data))[:-1]
        keys = ", ".join([ self.quote(k) for k in data.keys() ])
        quoted_table = self.quote(table_name)
        sql = f"INSERT INTO {quoted_table} ({keys}) VALUES ({valueStr})"
        if identity_insert:
            sql = "SET IDENTITY_INSERT " + quoted_table + " ON; " + sql + "; SET IDENTITY_INSERT " + quoted_table + " OFF"  
        self.insert_row_data(ttcxn, sql, data, table_name)

    def insert_row_data(self, ttcxn, sql, data, table_name):
        try:
            ttcxn.cursor().execute(sql, *list(data.values()))
        except pyodbc.DatabaseError as e:
            if "Cannot insert explicit value for identity column" in str(e):
                return self.insert_row(ttcxn, table_name, data, identity_insert=True)
            elif "conflicted with the FOREIGN KEY constraint" in str(e):
                raise
            else:
                from pprint import pformat
                sys.stderr.write("Failed to insert data into " + table_name + ":\n")
                sys.stderr.write(pformat(data) + "\n")
                raise

    def update_start_rv(self):
        try:
            with self.make_connection(self.database_name) as ttcxn:
                self.readrv(ttcxn)
        except pyodbc.Error as e:
            print("Unexpected error for update rv " + self.database_name + ":", e)
            pass
    
    def cursor(self):
        return self.cnxn.cursor()

    def query(self, s):
        return self.cursor().execute(s)

    def single(self):
        pass # no generic way to do this in sql

    def multi(self):
        pass # no generic way to do this in sql
    
    def query_single(self, q):
        try:
            self.single()
        except pyodbc.Error:
            print("Failed to go into single user mode.")
        try:
            self.query(q)
        finally:
            self.multi()
            
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.drop()

    def drop(self):
        if self.iscreated:
            self.single()
            try:
                self.query("DROP DATABASE " + self.database_name + ";")
                self.iscreated = False
            except pyodbc.Error as e:
                print("Unexpected error for drop db " + self.database_name + ":", e)
               
    def get_connection_string(self, driver=True):
        connstr = self.connectionStringTemplate % self.database_name
        if not driver:
            return connstr.split(";", 1)[-1]
        return connstr
                
    @classmethod
    def make_connection(cls, dbname):
        if cls.connectionStringTemplate is None:
            cls.connectionStringTemplate = cls.make_connection_string_template()
        connstr = cls.connectionStringTemplate % dbname
        return pyodbc.connect(connstr, autocommit=True)
            
    def readrv(self, ttcxn):
        pass # Really an MSSQL concept
        
    def readmax(self):
        if 'TEXTTEST_DUMPTABLES' not in os.environ:
            return
   
        dumpnames = os.environ['TEXTTEST_DUMPTABLES'].split(',')
    
        ttcnxn = self.make_connection(self.database_name)
        for descname in dumpnames:
            descparts = descname.split(':')
            tabname = descparts[0]
            maxcolname = descparts[1]
            notabmax = descparts[2]
            try:
                rows = ttcnxn.cursor().execute('select MAX(' + maxcolname + ') AS maxval FROM ' + tabname).fetchall()
                self.maxval[tabname] = rows[0].maxval
            except:
                self.maxval[tabname] = notabmax
        ttcnxn.close()
    
    def append_to_sql_query(self, column_tuple):
        if column_tuple[1] in [ "datetime", "image", "varbinary" ]:
            return "%s" % column_tuple[0]
        elif column_tuple[1] in [ "binary", "timestamp" ]:
            return "master.sys.fn_varbintohexstr(%s)" % column_tuple[0]
        else:
            return str(column_tuple[0])

    def get_row_data(self, row, column_names, col_name):
        for i, (name, _) in enumerate(column_names):
            if col_name == name:
                return str(row[i]).strip()

    def extract_blobs(self, column_value):
        return [ column_value ]

    def get_row_data_based_on_type(self, column_name, column_type, column_value):
        blobs = []
        try:
            if column_type in [ "image", "varbinary" ] and column_value is not None:
                try:
                    blobs = self.extract_blobs(column_value)
                    column_value_str = "<blob data>"
                except:
                    column_value_str = column_value
            elif column_type == "datetime" and column_value is not None:
                column_value_str = column_value.strftime("%Y-%m-%d %H:%M:%S")
            else:
                column_value_str = str(column_value)
        finally: 
            return "%s: %s" % (column_name, column_value_str), blobs

    def getColumnSortKey(self, coldata):
        # Column ordering can vary a lot, depending on how the db was created. We always show the columns in a standard order
        # The IDs come at the top, with other stuff sorted alphabetically
        name = coldata[0]
        if name.endswith("_id") or name == "id":
            return "000" + name
        elif name.endswith("_image"):
            return "zzz" + name
        else:
            return name
        
    def get_tables_dir_name(self):
        prefix = self.database_name.split("db_")[0]
        postfix = "db_tables"
        if prefix in [ "tt", "database" ]:
            return postfix
        else:
            return prefix + postfix
        
    def get_blob_patterns(self):
        return []
        
    def make_empty_tables_dir(self, writeDir):
        localName = self.get_tables_dir_name()
        dirName = os.path.join(writeDir, localName)
        if os.path.isdir(dirName):
            shutil.rmtree(dirName)
        dirsToMake = set()
        blob_patterns = []
        for blob_pattern_local in self.get_blob_patterns():
            pattern = os.path.normpath(os.path.join(dirName, blob_pattern_local))
            blob_patterns.append(pattern)
            dirsToMake.add(os.path.dirname(pattern))
            
        if len(dirsToMake) == 0:
            dirsToMake.add(dirName)
        for d in dirsToMake:
            os.makedirs(d)
        table_file_pattern = os.path.join(dirName, "${table_name}.table")
        return table_file_pattern, blob_patterns
        
    def write_data_subset(self, writeDir, subset_data):
        table_file_pattern, blob_patterns = self.make_empty_tables_dir(writeDir)
        table_data = {}
        with self.make_connection(self.database_name) as ttcxn:
            for tablespec, constraint in subset_data:
                print("Getting data for table(s)", repr(tablespec), ",", repr(constraint))
                rows, colnames = self.extract_data_for_dump(ttcxn, tablespec, constraint)
                if len(rows) > 0:
                    self.store_table_data(table_data, tablespec, rows, colnames)
        for tablename, (rows, colnames) in table_data.items():
            self.write_dump_data(rows, colnames, tablename, table_file_pattern, blob_patterns)
            
    def store_table_data(self, table_data, tablespec, rows, colnames):
        if "," not in tablespec:
            table_data[tablespec] = list(map(tuple, rows)), colnames 
            return
                
        for tablename in tablespec.split(","):
            tablecolnames = []
            tablecolindices = []
            for ix, (colname, coltype) in enumerate(colnames):            
                currtable, colname = colname.split(".", 1)
                if currtable == tablename:
                    tablecolnames.append((colname, coltype))
                    tablecolindices.append(ix)
            if tablename in table_data:
                tablerows, _ = table_data.get(tablename)
            else:
                tablerows = []
                table_data[tablename] = tablerows, tablecolnames
            firstcol = min(tablecolindices)
            lastcol = max(tablecolindices)
            for row in rows:
                newRow = row[firstcol:lastcol + 1]
                if newRow not in tablerows:
                    tablerows.append(newRow)
                    
    def write_all_tables(self, table_file_pattern, blob_pattern, ttcxn):
        for tablename in self.get_table_names(ttcxn):
            print("Making file for table", repr(tablename))
            self.dumptable(ttcxn, tablename, "", table_file_pattern, blob_pattern)

    def write_data(self, writeDir, use_master_connection=False):
        table_file_pattern, blob_pattern = self.make_empty_tables_dir(writeDir)
        if use_master_connection:
            self.write_all_tables(table_file_pattern, blob_pattern, self.cnxn)
        else:
            with self.make_connection(self.database_name) as ttcxn:
                self.write_all_tables(table_file_pattern, blob_pattern, ttcxn)

    def get_table_names(self, ttcxn):
        cursor = ttcxn.cursor()
        return [ row.table_name for row in cursor.tables(tableType="TABLE") ]
    
    def in_exclude_patterns(self, tn, patterns):
        return any((fnmatch(tn, pattern) for pattern in patterns))

    def expand_table_names(self, ttcxn, table_str, exclude):
        tables = []
        exclude_names = exclude.split(',')
        for pattern in table_str.split(','):
            if "*" in pattern:
                for tn in self.get_table_names(ttcxn):
                    if fnmatch(tn, pattern):
                        tables.append(tn)
            else:
                tables.append(pattern)
        if "*" in exclude:
            return [t for t in tables if not self.in_exclude_patterns(t, exclude_names) ]
        else:
            return [t for t in tables if t not in exclude_names]

    def get_blob_patterns_for_dump(self, sut_ext):
        return []

    def dumptables(self, sut_ext, table_str, usemaxcol='rv', exclude="", dumpwholenamestr="", dumpableBlobs=True):
        dumpwholenames = dumpwholenamestr.split(',')
        with self.make_connection(self.database_name) as ttcxn:
            for descname in self.expand_table_names(ttcxn, table_str, exclude):
                descparts = descname.split(':')
                tablename = descparts[0]
                table_fn_pattern = 'db_${table_name}.' + sut_ext
                maxval = self.startrv
                if tablename in self.maxval:
                    maxval = "'" + self.maxval[tablename] + "'"
                    usemaxcol = descparts[1]
                dumpwhole = tablename in dumpwholenames
                constraint = 'WHERE ' + usemaxcol + ' > ' + maxval if usemaxcol and not dumpwhole else ""
                blob_patterns = self.get_blob_patterns_for_dump(sut_ext)
                self.dumptable(ttcxn, tablename, constraint, table_fn_pattern, blob_patterns, dumpableBlobs)
                
    def get_column_names_for_spec(self, ttcxn, tablespec):
        if "," in tablespec:
            colnames = []
            table_names = tablespec.split(",")
            timestamp_col = None
            for table_name in table_names:
                table_col_names, table_timestamp_col = self.get_column_names(ttcxn, table_name)
                for colname, coltype in table_col_names:
                    colnames.append((table_name + "." + colname, coltype))
                if timestamp_col is None and table_timestamp_col is not None:
                    timestamp_col = table_name + "." + table_timestamp_col
            return colnames, timestamp_col
        else:
            return self.get_column_names(ttcxn, tablespec)
        
    def get_column_index(self, ttcxn, tablename, colname):
        cols = ttcxn.cursor().columns(table=tablename)
        for i, col in enumerate(cols):
            if col.column_name == colname:
                return i
    
    def get_column_names(self, ttcxn, tablename):
        cols = self.query_for_columns(ttcxn, tablename)
        colnames = []
        timestampcol = None 
        include_timestamp_var = os.getenv("DB_TABLE_DUMP_INCLUDE_TIMESTAMP")
        include_timestamp_tables = []
        if include_timestamp_var:
            include_timestamp_tables = include_timestamp_var.split(',')
        for col in cols:
            if col.type_name == "timestamp":
                timestampcol = col.column_name
                if tablename in include_timestamp_tables:
                    colnames.append((col.column_name, col.type_name))
            if col.type_name != "timestamp":
                colnames.append((col.column_name, col.type_name))
        
        colnames.sort(key=self.getColumnSortKey)
        return colnames, timestampcol

    def query_for_columns(self, ttcxn, tablename):
        return ttcxn.cursor().columns(table=tablename)

    def quote(self, tablespec):
        return '"' + tablespec + '"'
     
    def extract_data_for_dump(self, ttcxn, tablespec, constraint=""):
        colnames, usemaxcol = self.get_column_names_for_spec(ttcxn, tablespec)
        if len(colnames) == 0:
            return [], []
        select_values = [ self.append_to_sql_query(col) for col in colnames ]
        sqltext = 'SELECT '+ ",".join(select_values) + ' from ' + self.quote(tablespec) + ' ' + constraint
        if usemaxcol:
            sqltext += ' ORDER BY ' + usemaxcol
        try:
            rows = ttcxn.cursor().execute(sqltext).fetchall()
        except pyodbc.DatabaseError as e:
            if "Invalid column name 'rv'" in str(e):
                # Table has no rv, dump the constraint and assume the whole table is relevant
                return self.extract_data_for_dump(ttcxn, tablespec)
            else:
                sys.stderr.write(f"ERROR: could not write table(s) {tablespec} due to problems with query:\n{sqltext}\n")
                sys.stderr.write(str(e) + "\n")
                return [], colnames
        
        return rows, colnames
     
    def dumptable(self, ttcxn, tablename, constraint, table_fn_pattern, blob_pattern, dumpableBlobs=True):
        rows, colnames = self.extract_data_for_dump(ttcxn, tablename, constraint) 
        if len(rows) > 0:
            self.write_dump_data(rows, colnames, tablename, table_fn_pattern, blob_pattern, dumpableBlobs)
        
    def write_dump_data(self, rows, colnames, tablename, table_fn_pattern, blob_patterns, dumpableBlobs=True):
        fileName = Template(table_fn_pattern).substitute(table_name=tablename)
        with codecs.open(fileName, mode='a', encoding='cp1252', errors='replace') as f:
            # Note "codecs.open" implicitly opens files in binary mode! Hence the normal handling of "\n" is disabled
            # Use os.linesep instead or we get unix line endings...
            for il, row in enumerate(rows):
                header = "ROW:%s" % str(il) + os.linesep
                f.write(header)
                blobs = []
                for ci, (colname, coltype) in enumerate(colnames):
                    fdata, currBlobs = self.get_row_data_based_on_type(colname, coltype, row[ci])
                    value = '   %s' % fdata + os.linesep
                    f.write(value)
                    blobs += currBlobs
                if dumpableBlobs and blobs:
                    self.dumpblobs(blobs, blob_patterns, row, colnames)
                    
    def get_blob_file_name(self, fileNameData, blob_patterns):
        for blob_pattern in blob_patterns:
            fn = Template(blob_pattern).safe_substitute(fileNameData)
            if "$" not in fn:
                return fn, os.path.dirname(blob_pattern)
            
        sys.stderr.write("Failed to find blob given patterns " + repr(blob_patterns) + " and " + repr(fileNameData) + "\n")
        return None, None

    def dumpblobs(self, blobs, blob_patterns, row, column_names):
        class FileNameData:
            def __getitem__(innerself, key): # @NoSelf
                return self.get_row_data(row, column_names, key)
            
            def __contains__(innerself, key): # @NoSelf
                return self.get_row_data(row, column_names, key) is not None
            
        fileNameData = FileNameData()
        for b in blobs:
            blobFileName, _ = self.get_blob_file_name(fileNameData, blob_patterns)
            if blobFileName:
                with open(blobFileName, "wb") as f:
                    f.write(b)
                    
                    
class MSSQL_DBText(DBText):
    def handle_datetimeoffset(self, dto_value):
        # ref: https://github.com/mkleehammer/pyodbc/issues/134#issuecomment-281739794
        tup = struct.unpack("<6hI2h", dto_value)  # e.g., (2017, 3, 16, 10, 35, 18, 0, -6, 0)
        tweaked = [tup[i] // 100 if i == 6 else tup[i] for i in range(len(tup))]
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}.{:07d} {:+03d}:{:02d}".format(*tweaked)
    
    def get_create_db_args(self, mdffile=None):
        localdbFolder = os.getenv("TEXTTEST_SANDBOX")
        if mdffile:
            return " ON (FILENAME = '" + mdffile + "') FOR ATTACH_REBUILD_LOG"
        elif localdbFolder:
            if os.name == "nt":
                localdbFolder = localdbFolder.replace('/','\\')
            tmpDbFileName = os.path.join(localdbFolder, self.database_name + ".mdf")
            return " ON (NAME = '" + self.database_name + "', FILENAME='" + tmpDbFileName + "')"
        else:
            return ""
        
    def extract_data_for_dump(self, ttcxn, *args, **kw):
        ttcxn.add_output_converter(-155, self.handle_datetimeoffset)
        return super().extract_data_for_dump(ttcxn, *args, **kw)
    
    def single(self):
        try:
            self.query("ALTER DATABASE " + self.database_name + " SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        except pyodbc.Error as e:
            print("Unexpected error for alter db " + self.database_name + ":", e)
        
    def multi(self):
        self.query("ALTER DATABASE " + self.database_name + " SET MULTI_USER")
        
    def readrv(self, ttcxn):
        rows = ttcxn.cursor().execute('select master.sys.fn_varbintohexstr(@@DBTS) AS maxrv').fetchall()
        self.startrv = rows[0].maxrv
    
    @classmethod
    def get_driver(cls):
        odbc, legacy = [], []
        for driver in pyodbc.drivers():
            if driver.startswith("ODBC Driver"):
                odbc.append(driver)
            elif driver.startswith("SQL Server"):
                legacy.append(driver)

        if odbc:
            return max(odbc)
        elif legacy:
            return max(legacy)
        else:
            raise RuntimeError("No suitable drivers found for SQL Server LocalDB, is it installed?")
    
    @classmethod
    def get_localdb_server(cls):
        proc = subprocess.Popen([ "SqlLocalDB", "info"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.communicate()[0]
        installed = [ str(line.strip(), locale.getpreferredencoding()) for line in out.splitlines() ]
        if cls.enforceVersion is not None:
            candidates = [ cls.enforceVersion ]
        else:
            candidates = [ "MSSQLLocalDB", "v11.0" ]
        for candidate in candidates:
            if candidate in installed:
                return candidate
        
        raise RuntimeError("No recognised default LocalDB instance found, is it installed correctly?")
               
    @classmethod
    def make_connection_string_template(cls):
        driver = cls.get_driver()
        server = cls.get_localdb_server()
        return 'DRIVER={' + driver + '};SERVER=(localdb)\\' + server + ';Integrated Security=true;DATABASE=%s;'
    
    
class MySQL_DBText(DBText):

    def __init__(self, database=None, master_connection=None, ansi_sql_mode=False):
        """
        Use this class when the database you want to set up for testing is MySQL
        :param database: the name of the database to create for testing. You should give a name that is unique to your test case run, for example include the current process id in the name
        :param master_connection: a connection to a database that already exists, that dbtext can use to create new databases.
        By default it will try to connect to a database named 'master'. If one doesn't exist, you could just create an empty one with that name.
        :param ansi_sql_mode: if the MySQL database is configured to have ANSI mode you should set this flag since it affects the syntax of the SQL you use
        (see https://dev.mysql.com/doc/refman/5.7/en/sql-mode.html for more information about modes)
        """
        super().__init__(database, master_connection)
        self.ansi_sql_mode=ansi_sql_mode

    def quote(self, tablespec):
        if self.ansi_sql_mode:
            return super().quote(tablespec)
        else:
            # A default installation of MySQL does not use ANSI mode and uses backticks to escape reserved words in column names etc
            return '`' + tablespec + '`'

    @classmethod
    def get_driver(cls):
        drivers = []
        for driver in pyodbc.drivers():
            if driver.startswith("MySQL"):
                drivers.append(driver)

        if drivers:
            return max(drivers)
        else:
            raise RuntimeError("No suitable drivers found for MySQL, is it installed?")
               
    @classmethod
    def make_connection_string_template(cls):
        driver = cls.get_driver()
        return 'DRIVER={' + driver + '};SERVER=localhost;USER=root;OPTION=3;DATABASE=%s;'


class Sqlite3_DBText(DBText):

    @classmethod
    def make_connection(cls, dbname):
        return sqlite3.connect(f"{dbname}.db")

    def create_empty_db(self):
        pass

    def drop(self):
        pass

    def execute_setup_query(self, ttcxn, currQuery):
        ttcxn.executescript(currQuery)

    def get_table_names(self, ttcxn):
        cursor = ttcxn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [name[0] for name in cursor.fetchall()]
        return tables

    def query_for_columns(self, ttcxn, tablename):
        cursor = ttcxn.cursor()
        cursor.execute(f"PRAGMA TABLE_INFO({tablename})")

        class Sqlite3Column:
            def __init__(self, pragma_data):
                self.column_name = pragma_data[1]
                self.type_name = pragma_data[2].lower()
            def __repr__(self):
                return f"Sqlite3Column({self.column_name}, {self.type_name})"

        cols = [Sqlite3Column(pragma_data) for pragma_data in cursor.fetchall()]
        return cols

    def insert_row_data(self, ttcxn, sql, data, table_name):
        ttcxn.cursor().execute(sql, list(data.values()))





