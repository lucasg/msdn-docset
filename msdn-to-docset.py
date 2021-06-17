#!/usr/bin/env python3

import sqlite3
import os
import sys
import glob
import re
import shutil
import logging
import json
import tarfile
import tempfile
import argparse
import urllib.parse
import urllib
import time
import collections

from ruamel.yaml import YAML
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from requests.exceptions import ConnectionError
from bs4 import BeautifulSoup as bs, Tag # pip install bs4
from selenium import webdriver
# from selenium.webdriver import Firefox
# from selenium.webdriver.firefox.options import Options
# from selenium.webdriver.firefox.firefox_binary import FirefoxBinary

from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options

class PoshWebDriver:
    """ Thin wrapper for selenium webdriver for page content retrieval """

    def __init__(self, executable_path = None):

        self.options = Options()
        self.options.add_argument("--headless")
        self.options.add_argument("--window-size=1920x1080")

        self.driver = webdriver.Chrome(options=self.options)


    def get_url_page(self, url):
        """ retrieve the full html content of a page after Javascript execution """
        
        index_html = None
        try:
            self.driver.get(url)
            index_html = self.driver.page_source
        except (ConnectionResetError, urllib.error.URLError) as e:
            # we may have a triggered a anti-scraping time ban
            # Lay low for several seconds and get back to it.

            self.driver.quit()
            time.sleep(2)
            

            self.driver = webdriver.Chrome(options=self.options)
                
            index_html = None

        # try a second time, and raise error if fail
        if not index_html:
            self.driver.get(url)
            index_html = self.driver.page_source

        return index_html

    def quit():
        return self.driver.quit()


class Configuration:

    # STATIC CONSTANTS
    # posh_doc_api_version = '0.2' # powershell doc api version, not this docset one.
    # posh_version = '6'
    docset_name = 'MSDN'

    domain = "docs.microsoft.com"
    base_url = "%s/en-us/powershell/module" % domain
    default_url = "https://%s/?view=powershell-%%s" % (base_url)
    default_theme_uri = "_themes/docs.theme/master/en-us/_themes"
    
    def __init__(self, args):

        
        # # selected powershell api version
        # self.powershell_version = args.version

        # # The modules and cmdlets pages are "versionned" using additional params in the GET request
        # self.powershell_version_param = "view=powershell-{0:s}".format(self.powershell_version)

        # build folder (must be cleaned afterwards)
        # self.build_folder = os.path.join(os.getcwd(), "_build_{0:s}".format(self.powershell_version))
        self.build_folder = os.path.join(os.getcwd(), "_build_msdn")

        # output file
        self.output_filepath = os.path.realpath(args.output)

        # powershell docs start page
        self.api_index_url = "https://docs.microsoft.com/en-us/windows/win32/api/"


        self.docs_index_url = "https://docs.microsoft.com/en-us/windows/win32/desktop-app-technologies"
        

        # # powershell docs table of contents url
        # self.docs_toc_url =  "https://{0:s}/psdocs/toc.json?{2:s}".format(
        #     Configuration.base_url, 
        #     self.powershell_version,
        #     self.powershell_version_param
        # )

        # self.windows_toc_url = "https://{0:s}/windowsserver2019-ps/toc.json?view=windowsserver2019-ps".format(
        #     Configuration.base_url
        # )

        # selenium webdriver
        self.webdriver = PoshWebDriver()

        # selected module
        # self.filter_modules = [module.lower() for module in args.modules]


# Global session for several retries
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[ 502, 503, 504 ])
session.mount('http://', HTTPAdapter(max_retries=retries))


def download_binary(url, output_filename):
    """ Download GET request as binary file """
    global session
    
    logging.debug("download_binary : %s -> %s" % (url, output_filename))

    # ensure the folder path actually exist
    os.makedirs(os.path.dirname(output_filename), exist_ok = True)

    r = session.get(url, stream=True)
    with open(output_filename, 'wb') as f:
        for data in r.iter_content(32*1024):
            f.write(data)

def download_textfile(url : str ,  output_filename : str, params : dict = None):
    """ Download GET request as utf-8 text file """
    global session

    logging.debug("download_textfile : %s -> %s" % (url, output_filename))

    # ensure the folder path actually exist
    os.makedirs(os.path.dirname(output_filename), exist_ok = True)
    
    while True:
        try:
            r = session.get(url, data = params)
        except ConnectionError:
            logging.debug("caught ConnectionError, retrying...")
            time.sleep(2)
        else:
            break
    
    r.encoding = 'utf-8'
    with open(output_filename, 'w', encoding="utf-8") as f:
        f.write(r.text)


def make_docset(source_dir, dst_filepath, filename):
    """ 
    Tar-gz the build directory while conserving the relative folder tree paths. 
    Copied from : https://stackoverflow.com/a/17081026/1741450 
    """
    dst_dir = os.path.dirname(dst_filepath)
    tar_filepath = os.path.join(dst_dir, '%s.tar' % filename)
    
    with tarfile.open(tar_filepath, "w:gz") as tar:
        tar.add(source_dir, arcname=os.path.basename(source_dir))

    shutil.move(tar_filepath, dst_filepath)
    


def download_page_contents(configuration, uri, output_filepath):
    """ Download a page using it's uri from the TOC """

    # Resolving "absolute" url et use appropriate version
    full_url = urllib.parse.urljoin(configuration.docs_toc_url, uri)
    versionned_url = "{0:s}?{1:s}".format(full_url, configuration.powershell_version_param) 

    download_textfile(versionned_url, output_filepath)
    

def download_module_contents(configuration, module_name, module_uri, module_dir, cmdlets, root_dir):
    """ Download a modules contents """
    
    module_filepath = os.path.join(module_dir, "%s.html" % module_name)

    logging.debug("downloading %s module index page  -> %s" % (module_name, module_filepath))
    if module_uri:
        download_page_contents(configuration, module_uri, module_filepath)

    cmdlets_infos = []

    # Downloading cmdlet contents
    for cmdlet in cmdlets:

        cmdlet_name = cmdlet['toc_title']
        if cmdlet_name.lower() in ("about", "functions", "providers", "provider"): # skip special toc
            continue

        cmdlet_uri = cmdlet["href"]
        cmdlet_filepath = os.path.join(module_dir, "%s.html" % cmdlet_name)

        logging.debug("downloading %s cmdlet doc -> %s" % (cmdlet_name, cmdlet_filepath))
        download_page_contents(configuration, cmdlet_uri, cmdlet_filepath)

        cmdlets_infos.append({
            'name' : cmdlet_name,
            'path' : os.path.relpath(cmdlet_filepath, root_dir),
        })

    module_infos = {
        'name' : module_name,
        'index' : os.path.relpath(module_filepath, root_dir),
        'cmdlets' : cmdlets_infos
    }

    return module_infos

def _findname(obj, key):
    if obj.get('href', None)==key:return obj['toc_title']
    for k, v in obj.items():
        if isinstance(v,dict):
            item = _findname(v, key)
            if item is not None:
                return item
        if isinstance(v,list):
            for i in v:
                item = _findname(i, key)
                if item is not None:
                    return item

def crawl_msdn_contents(configuration: Configuration, download_dir : str, ):
    """ Download MSDN modules and content pages based on TOC """

    # Download top-level toc for module
    # toc_url = "https://raw.githubusercontent.com/MicrosoftDocs/win32-pr.fr-fr/live/desktop-src/toc.yml"
    # logging.debug("Downloading msdn root toc : %s" % (toc_url))
    # r = requests.get(toc_url)

    # yaml=YAML(typ="safe")
    # top_level_toc = yaml.load(r.text)

    # modules_toc is a web based TOC, where as content_toc is file based
    content_toc = {}

    counter = 0
    for r, d, f in os.walk("/mnt/o/MSDN-Offline/win32/desktop-src/", topdown=True):
        # import pdb;pdb.set_trace();
        # if counter >=250:
        #     break

        for markdown_file in filter(lambda s: os.path.splitext(s)[1] == ".md" ,f):
                realarb = os.path.relpath(r, "/mnt/o/MSDN-Offline/win32/desktop-src/")
                # print(realarb)
                # print(markdown_file)

               
                url = "https://docs.microsoft.com/en-us/windows/win32/{0:s}/{1:s}".format(
                    realarb,
                    os.path.splitext(markdown_file)[0]
                )

                page_dir = os.path.join(download_dir, "docs.microsoft.com/win32", realarb)
                filepath = os.path.join(page_dir, "%s.html" % os.path.splitext(markdown_file)[0])
                logging.info("[+] download page %s  -> %s " % (url, filepath))
                download_textfile(url, filepath)

                # don't care about top level pages
                if realarb == '.':
                    continue

                if realarb not in content_toc.keys():

                    # download toc for page
                    toc_url = "https://docs.microsoft.com/en-us/windows/win32/{0:s}/toc.json".format(
                        realarb
                    )
                    logging.info("[+] download toc for page %s" % (toc_url))
                    toc_r = requests.get(toc_url)
                    if toc_r.status_code != 200:
                        content_toc[realarb] =  {
                            'name' : realarb,
                            'index' : os.path.join(os.path.relpath(page_dir, download_dir), "%s.html" % os.path.splitext(markdown_file)[0]),
                            'cmdlets' : [{
                                'name' : os.path.splitext(markdown_file)[0],
                                'path' : os.path.relpath(filepath, download_dir),
                            }],
                            'toc' : {'items' : [{}]}
                        }

                    else:
                        component_toc = json.loads(requests.get(toc_url).text)

                        component_title = component_toc['items'][0]['toc_title']
                        component_href = component_toc['items'][0]['href']


                        content_toc[realarb] =  {
                            'name' : component_title,
                            'index' : os.path.join(os.path.relpath(page_dir, download_dir), "%s.html" % component_href),
                            'cmdlets' : [{
                                'name' : os.path.splitext(markdown_file)[0],
                                'path' : os.path.relpath(filepath, download_dir),
                            }],
                            'toc' : component_toc
                        }
                else:

                    page_title =  _findname(content_toc[realarb]['toc']['items'][0], os.path.splitext(markdown_file)[0])
                    if not page_title:
                        page_title = os.path.splitext(markdown_file)[0]

                    content_toc[realarb]['cmdlets'].append({
                        'name' :page_title,
                        'path' : os.path.relpath(filepath, download_dir),
                    })

                # counter+=1

                # if counter >=250:
                #     break

                # import pdb;pdb.set_trace();


    # logging.debug("raw modules : %s" % [m['toc_title'] for m in modules_toc['items'][0]['children']])

    # optional filter on selected module
    # modules = modules_toc['items'][0]['children']
    # if len(configuration.filter_modules):
    #     modules = list(filter(lambda m: m['toc_title'].lower() in configuration.filter_modules, modules))
    #     logging.debug("filtered modules : %s" % [m['toc_title'] for m in modules])

    # Downloading modules contents
    # for module in modules:

    #     module_name = module['toc_title']
    #     module_uri = module.get("href")
    #     module_cmdlets = module['children']
    #     module_dir = os.path.join(download_dir, Configuration.base_url, module_name)

    #     logging.info("[+] download module %s" % (module_name))
    #     module_infos = download_module_contents(configuration, module_name, module_uri, module_dir,  module_cmdlets, download_dir)
    #     content_toc[module_name] = module_infos

    return content_toc

def rewrite_soup(configuration : Configuration, soup, html_path : str, documents_dir : str):
    """ rewrite html contents by fixing links and remove unnecessary cruft """

    # Fix navigations links
    links = soup.findAll("a", { "data-linktype" : "relative-path"}) # for modules and cmdlet pages
    link_pattern = re.compile(r"([\w\.\/-]+)")

    for link in links:

        href = link['href']
        fixed_href = href

        # go back to module
        # if href == "./?view=powershell-%s" % configuration.powershell_version:
        #     fixed_href = "./%s.html" % link.text

        # go to a relative page
        targets = link_pattern.findall(href)
        if not len(targets): # badly formated 'a' link
            continue

        module_name = targets[0]
        fixed_href = "%s.html" % module_name
    
        if fixed_href != href:
            logging.info("link rewrite : %s -> %s " % ( href, fixed_href))
            link['href'] = fixed_href

    # remove link to external references since we can't support it
    for abs_href in soup.findAll("a", { "data-linktype" : "absolute-path"}):
        abs_href.replace_with(abs_href.text)

    # remove unsupported nav elements
    nav_elements = [
        ["nav"  , { "class" : "doc-outline", "role" : "navigation"}],
        ["ul"   , { "class" : "breadcrumbs", "role" : "navigation"}],
        ["div"  , { "class" : "sidebar", "role" : "navigation"}],
        ["div"  , { "class" : "dropdown dropdown-full mobilenavi"}],
        ["p"    , { "class" : "api-browser-description"}],
        ["div"  , { "class" : "api-browser-search-field-container"}],
        ["div"  , { "class" : "pageActions"}],
        ["div"  , { "class" : "container footerContainer"}],
        ["div"  , { "class" : "dropdown-container"}],
        ["div"  , { "class" : "binary-rating-buttons"}],
        ["div"  , { "data-bi-name" : "pageactions"}],
        ["div"  , { "class" : "page-action-holder"}],
        ["div"  , { "class" : "header-holder"}],
        ["footer" , { "data-bi-name" : "footer", "id" : "footer"}],
        ["div"  , { "class" : "binary-rating-holder"}],
        ["div"  , { "id" : "left-container"}],
    ]

    for nav in nav_elements:
        nav_class, nav_attr = nav
        
        for nav_tag in soup.findAll(nav_class, nav_attr):
            _ = nav_tag.extract()

    # remove script elems
    for head_script in soup.head.findAll("script"):
            _ = head_script.extract()
    
    # Extract and rewrite additionnal stylesheets to download
    ThemeResourceRecord = collections.namedtuple('ThemeResourceRecord', 'url, path')

    theme_output_dir = os.path.join(documents_dir, Configuration.domain)
    theme_resources = []

    for link in soup.head.findAll("link", { "rel" : "stylesheet"}):
        uri_path = link['href'].strip()

        if not uri_path.lstrip('/').startswith(Configuration.default_theme_uri):
            continue

        # Construct (url, path) tuple
        css_url = "https://%s/%s" % (Configuration.domain, uri_path)
        css_filepath =  os.path.join(theme_output_dir, uri_path.lstrip('/'))

        # Converting href to a relative link
        path = os.path.relpath(css_filepath, os.path.dirname(html_path))
        rel_uri = '/'.join(path.split(os.sep))
        link['href'] = rel_uri

        theme_resources.append( ThemeResourceRecord( 
            url = css_url, 
            path = os.path.relpath(css_filepath, documents_dir), # stored as relative path
        ))

    return soup, set(theme_resources)

def rewrite_index_soup(configuration : Configuration, soup, index_html_path : str, documents_dir : str):
    """ rewrite html contents by fixing links and remove unnecessary cruft """

    # Fix navigations links
    content_tables = soup.findAll("table", { 
        "class" : "api-search-results"
    })

    for content_table in content_tables:

        links = content_table.findAll(lambda tag: tag.name == 'a')
        link_pattern = re.compile(r"/powershell/module/([\w\.\-]+)/\?view=powershell-")

        for link in links:

            href = link['href']
            fixed_href = href


            targets = link_pattern.findall(href)
            if not len(targets): 
                continue # badly formated 'a' link

            module_name = targets[0].lstrip('/').rstrip('/')
            fixed_href = "powershell/module/%s/%s.html" % (module_name, module_name)
            
            if fixed_href != href:
                logging.debug("link rewrite : %s -> %s " % ( href, fixed_href))
                link['href'] = fixed_href

        # Fix link to module.svg
        # module_svg_path = os.path.join(documents_dir, Configuration.domain, "en-us", "media", "toolbars", "module.svg")
        # images = content_table.findAll("img" , {'alt' : "Module"})
        # for image in images:
        #     image['src'] =  os.path.relpath(module_svg_path, os.path.dirname(index_html_path))

    # remove unsupported nav elements
    nav_elements = [
        ["nav"  , { "class" : "doc-outline", "role" : "navigation"}],
        ["ul"   , { "class" : "breadcrumbs", "role" : "navigation"}],
        ["div"  , { "class" : "sidebar", "role" : "navigation"}],
        ["div"  , { "class" : "dropdown dropdown-full mobilenavi"}],
        ["p"    , { "class" : "api-browser-description"}],
        ["div"  , { "class" : "api-browser-search-field-container"}],
        ["div"  , { "class" : "pageActions"}],
        ["div"  , { "class" : "dropdown-container"}],
        ["div"  , { "class" : "container footerContainer"}],
        ["div"  , { "data-bi-name" : "header", "id" : "headerAreaHolder"}],
        ["div"  , { "data-bi-name" : "pageactions", "role": "complentary"}],
        ["div"  , { "class" : "page-action-holder"}],
        ["footer" , { "data-bi-name" : "footer", "id" : "footer"}],
        ["div"  , { "id" : "cookie-consent-holder"}],
        ["nav"  , {  "role" : "navigation"}],
        ["div"  , { "id" : "left-container"}],
        ["div"  , { "class" : "binary-rating-holder"}],
    ]

    for nav in nav_elements:
        nav_class, nav_attr = nav
        
        for nav_tag in soup.findAll(nav_class, nav_attr):
            _ = nav_tag.extract()

    # remove script elems
    for head_script in soup.head.findAll("script"):
            _ = head_script.extract()
    for body_async_script in soup.body.findAll("script", { "async" : "",  "defer" : ""}):
            _ = head_script.extract()

    # Fixing and downloading css stylesheets
    theme_output_dir = os.path.join(documents_dir, Configuration.domain)
    for link in soup.head.findAll("link", { "rel" : "stylesheet"}):
        uri_path = link['href'].strip()

        if not uri_path.lstrip('/').startswith(Configuration.default_theme_uri):
            continue

        # Construct (url, path) tuple
        css_url = "https://%s/%s" % (Configuration.domain, uri_path)
        css_filepath =  os.path.join(theme_output_dir, uri_path.lstrip('/'))

        # Converting href to a relative link
        path = os.path.relpath(css_filepath, os.path.dirname(index_html_path))
        rel_uri = '/'.join(path.split(os.sep))
        link['href'] = rel_uri

        download_textfile(css_url, css_filepath)

    return soup


def rewrite_html_contents(configuration : Configuration, html_root_dir : str):
    """ rewrite every html file downloaded """

    additional_resources = set()

    for html_file in glob.glob("%s/**/*.html" % html_root_dir, recursive = True):

        logging.debug("rewrite  html_file : %s" % (html_file))

        # Read content and parse html
        with open(html_file, 'r', encoding='utf8') as i_fd:
            html_content = i_fd.read()

        soup = bs(html_content, 'html.parser')
        
        # rewrite html
        soup, resources = rewrite_soup(configuration, soup, html_file, html_root_dir)
        additional_resources = additional_resources.union(resources)

        # Export fixed html
        fixed_html = soup.prettify("utf-8")
        with open(html_file, 'wb') as o_fd:
            o_fd.write(fixed_html)

    return additional_resources


def download_additional_resources(configuration : Configuration, documents_dir : str, resources_to_dl : set = set()):
    """ Download optional resources for "beautification """

    for resource in resources_to_dl:
        
        download_textfile(
            resource.url, 
            os.path.join(documents_dir, resource.path)
        )

    # Download index start page
    index_url = "https://docs.microsoft.com/en-us/windows/win32/desktop-app-technologies"
    index_filepath = os.path.join(documents_dir, Configuration.domain, "win32", "index.html")

    soup = bs( configuration.webdriver.get_url_page(index_url), 'html.parser')
    soup = rewrite_index_soup(configuration, soup, index_filepath, documents_dir)
    fixed_html = soup.prettify("utf-8")
    with open(index_filepath, 'wb') as o_fd:
        o_fd.write(fixed_html)


    # # Download module.svg icon for start page
    # icon_module_url  =     '/'.join(["https:/"   , Configuration.domain, "en-us", "media", "toolbars", "module.svg"])
    # icon_module_path = os.path.join(documents_dir, Configuration.domain, "en-us", "media", "toolbars", "module.svg")
    # download_binary(icon_module_url, icon_module_path)


def create_sqlite_database(configuration, content_toc, resources_dir, documents_dir):
    """ Indexing the html document in a format Dash can understand """

    def insert_into_sqlite_db(cursor, name, record_type, path):
        """ Insert a new unique record in the sqlite database. """
        try:
            cursor.execute('SELECT rowid FROM searchIndex WHERE path = ?', (path,))
            dbpath = cursor.fetchone()
            cursor.execute('SELECT rowid FROM searchIndex WHERE name = ?', (name,))
            dbname = cursor.fetchone()

            if dbpath is None and dbname is None:
                cursor.execute('INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?)', (name, record_type, path))
                logging.debug('DB add [%s] >> name: %s, path: %s' % (record_type, name, path))
            else:
                logging.debug('record exists')

        except:
            pass

    sqlite_filepath = os.path.join(resources_dir, "docSet.dsidx")
    if os.path.exists(sqlite_filepath):
        os.remove(sqlite_filepath)

    db = sqlite3.connect(sqlite_filepath)
    cur = db.cursor()
    cur.execute('CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT);')
    cur.execute('CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path);')

    
    for module_name, module in content_toc.items():

        # path should be unix compliant
        module_path = module['index'].replace(os.sep, '/')
        insert_into_sqlite_db(cur, module_name, "Component", module_path)

        for cmdlet in module['cmdlets']:
            
            cmdlet_name = cmdlet['name']
            if cmdlet_name == module_name:
                continue

            # path should be unix compliant
            cmdlet_path = cmdlet['path'].replace(os.sep, '/')

            insert_into_sqlite_db(cur, cmdlet_name, "Entry", cmdlet_path)
        

    # commit and close db
    db.commit()
    db.close()

def copy_folder(src_folder : str, dst_folder : str):
    """ Copy a full folder tree anew every time """

    def onerror(func, path, exc_info):
        """
        Error handler for ``shutil.rmtree``.

        If the error is due to an access error (read only file)
        it attempts to add write permission and then retries.

        If the error is for another reason it re-raises the error.

        Usage : ``shutil.rmtree(path, onerror=onerror)``
        """
        import stat

        if not os.path.exists(path):
            return

        if not os.access(path, os.W_OK):
            # Is the error an access error ?
            os.chmod(path, stat.S_IWUSR)
            func(path)
        else:
            raise

    shutil.rmtree(dst_folder,ignore_errors=False,onerror=onerror) 
    shutil.copytree(src_folder, dst_folder)

def merge_folders(src, dst):
    
    if os.path.isdir(src):
        
        if not os.path.exists(dst):
            os.makedirs(dst)
        
        for name in os.listdir(src):
            merge_folders(
                os.path.join(src, name),
                os.path.join(dst, name)
            )
    else:
        shutil.copyfile(src, dst)

def main(configuration : Configuration):

    # """ Scheme for content toc : 
    # {
    #     module_name : {
    #         'name' : str,
    #         'index' : relative path,
    #         'cmdlets' : [
    #             {
    #                 'name' : str,
    #                 'path' : relative path, 
    #             },
    #             ...
    #         ]
    #     },
    #     ...
    # }
    # """
    content_toc = {}
    resources_to_dl = set()

    """ 0. Prepare folders """
    download_dir = os.path.join(configuration.build_folder, "_1_downloaded_contents")
    html_rewrite_dir = os.path.join(configuration.build_folder, "_2_html_rewrite")
    additional_resources_dir = os.path.join(configuration.build_folder, "_3_additional_resources")
    package_dir = os.path.join(configuration.build_folder, "_4_ready_to_be_packaged")

    for folder in [download_dir, html_rewrite_dir, additional_resources_dir, package_dir]:
        os.makedirs(folder, exist_ok=True)

    # _4_ready_to_be_packaged is the final build dir
    docset_dir = os.path.join(package_dir, "%s.docset" % Configuration.docset_name)
    content_dir = os.path.join(docset_dir , "Contents")
    resources_dir = os.path.join(content_dir, "Resources")
    document_dir = os.path.join(resources_dir, "Documents")

    """ 1. Download html pages """
    logging.info("[1] scraping web contents")
    content_toc = crawl_msdn_contents(configuration, download_dir)

    # api_content_toc = crawl_msdn_contents(configuration, download_dir)

    # # do not download twice the win10 api since it's quite a handful
    # if os.path.exists(os.path.join(win10_download_dir, "toc.json")):
    #     with open(os.path.join(win10_download_dir, "toc.json"), "r") as content:
    #         windows_toc = json.load(content)
    # else:
    #     windows_toc = crawl_msdn_contents(configuration, win10_download_dir)
    #     with open(os.path.join(win10_download_dir, "toc.json"), "w") as content:
    #             json.dump(windows_toc, content)
        
    # Merge win10 api content
    # merge_folders(win10_download_dir, download_dir)
    # content_toc.update(windows_toc)
    # with open(os.path.join(download_dir, "toc.json"), "w") as content:
    #     json.dump(content_toc, content)

    """ 2.  Parse and rewrite html contents """
    logging.info("[2] rewriting urls and hrefs")
    copy_folder(download_dir, html_rewrite_dir)
    resources_to_dl = rewrite_html_contents(configuration, html_rewrite_dir)

    """ 3.  Download additionnal resources """
    logging.info("[3] download style contents")
    copy_folder(html_rewrite_dir, additional_resources_dir )
    download_additional_resources(configuration, additional_resources_dir, resources_to_dl)

    """ 4.  Database indexing """
    logging.info("[4] indexing to database")
    copy_folder(additional_resources_dir, document_dir )
    create_sqlite_database(configuration, content_toc, resources_dir, document_dir)

    """ 5.  Archive packaging """
    src_dir = os.path.dirname(__file__)
    shutil.copy(os.path.join(src_dir, "static/Info.plist"), content_dir)
    shutil.copy(os.path.join(src_dir, "static/DASH_LICENSE"), os.path.join(resources_dir, "LICENSE"))
    shutil.copy(os.path.join(src_dir, "static/icon.png"), docset_dir)
    shutil.copy(os.path.join(src_dir, "static/icon@2x.png"), docset_dir)

    output_dir = os.path.dirname(configuration.output_filepath)
    os.makedirs(output_dir, exist_ok=True)

    logging.info("[5] packaging as a dash docset")
    make_docset(
        docset_dir,
        configuration.output_filepath,
        Configuration.docset_name
    )


if __name__ == '__main__':

    

    parser = argparse.ArgumentParser(
        description="Dash docset creation script for MSDN's Win32 API"
    )

    parser.add_argument("-vv", "--verbose", 
        help="increase output verbosity", 
        action="store_true"
    )

    parser.add_argument("-t", "--temporary", 
        help="Use a temporary directory for creating docset, otherwise use current dir.", 
        default=False, 
        action="store_true"
    )

    parser.add_argument("-l", "--local", 
        help="Do not download content. Only for development use.\n" + 
             "Incompatible with --temporary option", 
        default=False, 
        action="store_true"
    )

    parser.add_argument("-o", "--output", 
        help="set output filepath", 
        default = os.path.join(os.getcwd(), "MSDN.tgz"),
    )

    # parser.add_argument("-p", "--phantom", 
    #     help="path to phantomjs executable", 
    #     default = None,
    # )

    # parser.add_argument("-m", "--modules", 
    #     help="filter on selected modules", 
    #     default = [],
    #     type=str,
    #     nargs='+'
    # )

    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO)

    conf = Configuration( args )

    if args.temporary:

        with tempfile.TemporaryDirectory() as tmp_builddir:
            conf.build_folder = tmp_builddir
            main(conf)
    else:
        main(conf)
