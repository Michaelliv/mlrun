# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import base64
import uuid
from io import BytesIO
import pandas as pd
from os import path
from .utils import is_ipython, get_in, dict_to_list

IPYTHON_ROOT = '/User'
V3IO_ROOT = 'files/v3io'


def html_dict(title, data, open=False, show_nil=False):
    if not data:
        return ''
    html = ''
    for key, val in data.items():
        if show_nil or val:
            html += f'<tr><th>{key}</th><td>{val}</td></tr>'
    if html:
        html = f'<table>{html}</table>'
        return html_summary(title, html, open=open)
    return ''


def html_summary(title, data, num=None, open=False):
    tag = ''
    if open:
        tag = ' open'
    if num:
        title = f'{title} ({num})'
    summary = '<details{}><summary><b>{}<b></summary>{}</details>'
    return summary.format(tag, title, data)


def html_crop(x):
    return f'<div class="ellipsis" ondblclick="copyToClipboard(this)" title="{x} (dbl click to copy)">{x}</div>'


def table_sum(title, df):
    size = len(df.index)
    if size > 0:
        return html_summary(title, df.to_html(escape=False), size)


def plot_to_html(fig):
    """ Convert Matplotlib figure 'fig' into a <img> tag for HTML use using base64 encoding. """
    from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

    canvas = FigureCanvas(fig)
    png_output = BytesIO()
    canvas.print_png(png_output)
    data = png_output.getvalue()

    data_uri = base64.b64encode(data).decode('utf-8')
    return '<img src="data:image/png;base64,{0}">'.format(data_uri)


def dict_html(x):
    return ''.join([f'<div class="dictlist">{i}</div>'
                    for i in dict_to_list(x)])


def link_to_ipython(link):
    ref = 'class="artifact" onclick="expandPanel(this)" paneName="result" '
    if '://' not in link:
        abs = path.abspath(link)
        if abs.startswith('/User'):
            return abs.replace(IPYTHON_ROOT, '/files'), ref
        else:
            return abs, ''
    elif link.lower().startswith('v3io:///'):
        return V3IO_ROOT + link[7:], ref
    return link, ''


def link_html(text, link=''):
    if not link:
        link = text
    link, ref = link_to_ipython(link)
    return '<div {}title="{}">{}</div>'.format(ref, link, text)


def artifacts_html(x, pathcol='path'):
    if not x:
        return ''
    html = ''
    for i in x:
        link, ref = link_to_ipython(i[pathcol])
        html += '<div {}title="{}">{}</div>'.format(ref, link, i['key'])
    return html


def run_to_html(results, display=True):
    html = html_dict('Metadata', results['metadata'])
    html += html_dict('Spec', results['spec'])
    html += html_dict('Outputs', results['status'].get('outputs'), True, True)

    if 'iterations' in results['status']:
        iter = results['status']['iterations']
        if iter:
            df = pd.DataFrame(iter[1:], columns=iter[0]).set_index('iter')
            html += table_sum('Iterations', df)

    artifacts = results['status'].get('output_artifacts', None)
    if artifacts:
        df = pd.DataFrame(artifacts)
        if 'description' not in df.columns.values:
            df['description'] = ''
        df = df[['key', 'kind', 'target_path', 'description']]
        df['target_path'] = df['target_path'].apply(link_html)
        html += table_sum('Artifacts', df)

    return ipython_display(html, display)


def ipython_display(html, display=True):
    if display and html and is_ipython:
        import IPython
        IPython.display.display(IPython.display.HTML(html))
    return html


style = """<style> 
.dictlist {
  background-color: #b3edff; 
  text-align: center; 
  margin: 4px; 
  border-radius: 3px; padding: 0px 3px 1px 3px; display: inline-block;}
.artifact {
  cursor: pointer; 
  background-color: #ffe6cc; 
  text-align: left; 
  margin: 4px; border-radius: 3px; padding: 0px 3px 1px 3px; display: inline-block;
}
div.block.hidden {
  display: none;
}
.clickable {
  cursor: pointer;
}
.ellipsis {
  display: inline-block;
  max-width: 60px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.master-wrapper {
  display: flex;
  flex-flow: row nowrap;
  justify-content: flex-start;
  align-items: stretch;
}
.master-wrapper > div {
  flex: 1 auto;
  margin: 4px;
  padding: 10px;
}
iframe.fileview {
  border: 0 none;
  height: 100%;
  width: 100%;
  white-space: pre-wrap;
}
.pane-header-title {
  width: 80%;
  font-weight: 500;
}
.pane-header {
  line-height: 1;
  background-color: #ffe6cc;
  padding: 3px;
}
.pane-header .close {
  font-size: 20px;
  font-weight: 700;
  float: right;
  margin-top: -5px;
}
.master-wrapper .right-pane {
  border: 1px inset silver;
  width: 40%;
  min-height: 300px;
}

.master-wrapper * {
  box-sizing: border-box;
}

</style>"""

jscripts = """<script>
function copyToClipboard(fld) {
    if (document.queryCommandSupported && document.queryCommandSupported('copy')) {
        var textarea = document.createElement('textarea');
        textarea.textContent = fld.innerHTML;
        textarea.style.position = 'fixed';
        document.body.appendChild(textarea);
        textarea.select();

        try {
            return document.execCommand('copy'); // Security exception may be thrown by some browsers.
        } catch (ex) {

        } finally {
            document.body.removeChild(textarea);
        }
    }
}
function expandPanel(el) {
  const panelName = "#" + el.getAttribute('paneName');
  console.log(el.title);

  document.querySelector(panelName + "-title").innerHTML = el.title
  iframe = document.querySelector(panelName + "-body");
  
  function reqListener () {
    iframe.setAttribute("srcdoc", this.responseText);
    console.log(this.responseText);
  }

  const oReq = new XMLHttpRequest();
  oReq.addEventListener("load", reqListener);
  oReq.open("GET", el.title);
  oReq.send();
  
  
  //iframe.src = el.title;
  const resultPane = document.querySelector(panelName + "-pane");
  if (resultPane.classList.contains("hidden")) {
    resultPane.classList.remove("hidden");
  }
}
function closePanel(el) {
  const panelName = "#" + el.getAttribute('paneName')
  const resultPane = document.querySelector(panelName + "-pane");
  if (!resultPane.classList.contains("hidden")) {
    resultPane.classList.add("hidden");
  }
}

</script>"""

tblframe = """
<div class="master-wrapper">
  <div class="block">{}</div>
  <div id="result-pane" class="right-pane block hidden">
    <div class="pane-header">
      <span id="result-title" class="pane-header-title">Title</span>
      <span onclick="closePanel(this)" paneName="result" class="close clickable">&times;</span>
    </div>
    <iframe class="fileview" id="result-body"></iframe>
  </div>
</div>
"""

def get_tblframe(df, display):
    table = tblframe.format(df.to_html(escape=False, index=False, notebook=True))
    rnd = 'result' + str(uuid.uuid4())[:8]
    html = style + jscripts + table.replace('="result', '="' + rnd)
    return ipython_display(html, display)


def runs_to_html(df, display=True):
    df['inputs'] = df['inputs'].apply(artifacts_html)
    df['artifacts'] = df['artifacts'].apply(lambda x: artifacts_html(x, 'target_path'))
    df['labels'] = df['labels'].apply(dict_html)
    df['parameters'] = df['parameters'].apply(dict_html)
    df['results'] = df['results'].apply(dict_html)
    df['start'] = df['start'].apply(lambda x: x.strftime("%b %d %H:%M:%S"))
    df['uid'] = df['uid'].apply(lambda x: '<div title="{}">...{}</div>'.format(x, x[-6:]))
    pd.set_option('display.max_colwidth', -1)
    get_tblframe(df, display)


def artifacts_to_html(df, display=True):
    def prod_htm(x):
        if not x or not isinstance(x, dict):
            return ''
        p = '{}/{}'.format(get_in(x, 'kind', ''), get_in(x, 'uri', ''))
        if 'owner' in x:
            p += ' by {}'.format(x['owner'])
        return '<div title="{}" class="producer">{}</div>'.format(p, get_in(x, 'name', 'unknown'))

    if 'tree' in df.columns.values:
        df['tree'] = df['tree'].apply(html_crop)
    df['path'] = df['path'].apply(link_html)
    df['hash'] = df['hash'].apply(html_crop)
    df['sources'] = df['sources'].apply(artifacts_html)
    df['labels'] = df['labels'].apply(dict_html)
    df['producer'] = df['producer'].apply(prod_htm)
    df['updated'] = df['updated'].apply(lambda x: x.strftime("%b %d %H:%M:%S"))
    pd.set_option('display.max_colwidth', -1)

    get_tblframe(df, display)