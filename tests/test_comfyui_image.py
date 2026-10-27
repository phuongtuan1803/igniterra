"""ComfyImage qua HTTP thật trên loopback: manifest, refs, chống submit lại và monitor."""
from __future__ import annotations

import io
import json
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import approve_gpu_manifest
from studio import costs, pipeline, providers
from studio.comfyui import service as cs
from studio.gpu import service as gs
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.contracts import OfferFilters, Quote
from studio.providers.base import Fatal, Unconfirmed
from studio.providers.direct.comfyui import ComfyImage
from studio.web import create_app

PROFILE = Path(__file__).resolve().parent.parent / 'workflows/flux2-klein-4b'
IMAGE = 'example/comfyui@sha256:' + '0' * 64


class Backend:
    def __init__(self):
        self.token = ''
        self.posts = []
        self.uploads = []
        self.history = {}
        self.mode = 'ok'
        self.pending = set()
        graph = json.loads((PROFILE / 'reference.json').read_text())
        self.info = {n['class_type']: {'input': {'required': {}}} for n in graph.values()}
        self.info['LoadImage'] = {'input': {'required': {'image': [['reference.png'], {}]}}}
        data = io.BytesIO()
        Image.new('RGB', (1088, 1920), '#527c9a').save(data, format='PNG')
        self.png = data.getvalue()


@pytest.fixture
def backend():
    state = Backend()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'image/png' if isinstance(body, bytes) else 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get('Authorization') != 'Bearer ' + state.token:
                return self.reply(401, {})
            path = urlparse(self.path).path
            if path == '/object_info':
                return self.reply(200, state.info)
            if path.startswith('/history/'):
                key = path.split('/')[-1]
                return self.reply(200, {key: state.history[key]} if key in state.history else {})
            if path == '/queue':
                return self.reply(200, {'queue_running': [[0, p, {}, {}, []] for p in state.pending], 'queue_pending': []})
            if path == '/view':
                return self.reply(500, {}) if state.mode == 'download_failed' else self.reply(200, state.png)
            return self.reply(404, {})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if self.headers.get('Authorization') != 'Bearer ' + state.token:
                return self.reply(401, {})
            if self.path == '/upload/image':
                message = BytesParser(policy=policy.default).parsebytes(
                    b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + raw)
                parts = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
                name = parts['image'].get_filename()
                folder = parts['subfolder'].get_payload(decode=True).decode()
                state.uploads.append((name, parts['image'].get_payload(decode=True)))
                if folder == '':  # Như LoadImage thật: os.listdir(input_dir), không đệ quy.
                    state.info['LoadImage']['input']['required']['image'][0].append(name)
                return self.reply(200, {'name': '../evil.png' if state.mode == 'bad_upload' else name,
                                        'subfolder': folder, 'type': 'input'})
            if self.path == '/prompt':
                body = json.loads(raw)
                state.posts.append(body)
                pid = body['prompt_id']
                if state.mode == 'pending':
                    state.pending.add(pid)
                else:
                    stamp = int(time.time() * 1000)
                    state.history[pid] = {'status': {'status_str': 'success', 'completed': True,
                        'messages': [['execution_start', {'timestamp': stamp}], ['execution_success', {'timestamp': stamp + 10}]]},
                        'outputs': {'13': {'images': [{'filename': 'frame.png', 'subfolder': '', 'type': 'output'}]}}}
                return self.reply(200, {'prompt_id': pid})
            return self.reply(404, {})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f'127.0.0.1:{server.server_port}'
    server.shutdown()
    thread.join(timeout=3)


@pytest.fixture
def setup_image(env, backend, monkeypatch):
    state, address = backend
    approve_gpu_manifest(env.store)
    provider = FakeGPUProvider()
    gpu = gs.GPUService(env.store, provider, image=IMAGE, portal={'scheme': 'http', 'timeout_s': 1})
    offer = gpu.search_offers(OfferFilters())[0]
    rental = gpu.rent(gpu.new_rental_id(), Quote(offer_id=offer.offer_id, dph_total=offer.dph_total,
                                               quoted_at=offer.quoted_at, confirmed=True), OfferFilters())
    provider.boot(rental.provider_instance_id, {'1111/tcp': address, '8188/tcp': address})
    assert gpu._move(rental, 'ready')
    state.token = gpu._password(rental.rental_id)
    service = cs.ComfyService(env.store, gpu, max_download_attempts=1)
    profile = service.import_image_profile(PROFILE)
    service.approve_manifest(profile['manifest_sha256'])
    config = {**env.config, 'adapters': {**env.config['adapters'], 'image': 'comfyui'},
              'gpu': {'provider': 'fake', 'image': IMAGE}, 'comfyui': {'max_download_attempts': 1, 'image': {
                  'rental_id': rental.rental_id, 'workflow_id': profile['text2image']['workflow_id'],
                  'reference_workflow_id': profile['reference']['workflow_id'], 'poll_s': .001, 'timeout_s': .1}}}
    monkeypatch.setattr(gs, 'from_config', lambda *_: gpu)
    return ComfyImage(config), service, gpu, config, state


def generate(adapter, out, *, prompt='a robot', refs=(), attempt=1):
    with costs.paid_call(out.parent / 'costs.jsonl', f't_0001:s01:keyframes:{attempt}', adapter.name, adapter.model):
        return adapter.generate(prompt, list(refs), '1080x1920', out)


def test_real_http_generation_records_run_verified_output_and_provenance(setup_image, tmp_path):
    adapter, service, gpu, config, state = setup_image
    candidate = generate(adapter, tmp_path / 'out')
    assert candidate.provider == 'comfyui' and candidate.source.startswith('comfyui:cr_')
    assert Image.open(candidate.path).size == (1088, 1920)
    run = service.runs()[0]
    assert run.status == 'succeeded' and service.run_detail(run.run_id)['outputs'][0].sha256
    graph = state.posts[0]['prompt']
    assert graph['4']['inputs']['text'] == 'a robot' and graph['9']['inputs']['width'] == 1088
    assert gpu.rental(run.rental_id).status == 'ready'
    assert [e.status for e in costs.read(tmp_path / 'costs.jsonl')] == ['pending', 'ok']
    assert costs.read(tmp_path / 'costs.jsonl')[-1].est_usd is None
    assert isinstance(providers.get('image', config), ComfyImage)


def test_same_attempt_never_resubmits_but_new_attempt_can_regenerate(setup_image, tmp_path):
    adapter, service, gpu, config, state = setup_image
    first = generate(adapter, tmp_path / 'out')
    second = generate(ComfyImage(config), tmp_path / 'again')
    assert first.source == second.source and len(state.posts) == 1
    with pytest.raises(Fatal, match='input khác'):
        generate(adapter, tmp_path / 'changed', prompt='a different robot')
    generate(adapter, tmp_path / 'retry', attempt=2)
    assert len(state.posts) == 2 and state.posts[0]['prompt_id'] != state.posts[1]['prompt_id']


def test_reference_is_uploaded_by_hash_and_connected_to_conditioning(setup_image, tmp_path):
    adapter, service, gpu, config, state = setup_image
    ref = tmp_path / 'sheet.png'
    ref.write_bytes(state.png)
    generate(adapter, tmp_path / 'out', refs=[ref])
    assert len(state.uploads) == 1 and state.uploads[0][1] == state.png
    graph = state.posts[0]['prompt']
    assert graph['14']['inputs']['image'].startswith('igniterra_ref_')
    assert '/' not in graph['14']['inputs']['image']
    assert graph['6']['inputs']['positive'] == ['17', 0]
    assert graph['17']['class_type'] == 'ReferenceLatent'


def test_upload_with_malicious_server_path_does_not_submit(setup_image, tmp_path):
    adapter, _, _, _, state = setup_image
    state.mode = 'bad_upload'
    ref = tmp_path / 'sheet.png'; ref.write_bytes(state.png)
    with pytest.raises(Fatal, match='đường dẫn'):
        generate(adapter, tmp_path / 'out', refs=[ref])
    assert state.posts == []


@pytest.mark.parametrize('node,values', [('1', {'unet_name': 'unreviewed.safetensors'}), ('6', {'model': ['99', 0]}),
                                         ('9', {'width': 1081}), ('14', {'image': '../../secret.png'})])
def test_bindings_cannot_change_dependencies_links_or_escape_input_folder(setup_image, node, values):
    _, service, _, config, state = setup_image
    image = config['comfyui']['image']
    with pytest.raises(cs.ComfyServiceError):
        service.bind_image_inputs(image['reference_workflow_id'], 1, {node: values}, 'request')
    assert state.posts == []


def test_template_must_be_approved_before_binding(env):
    service = cs.ComfyService(env.store, None)
    profile = service.import_image_profile(PROFILE)
    assert service.revision(profile['text2image']['workflow_id']).status == 'pending_review'
    with pytest.raises(cs.ComfyServiceError, match='pending_review'):
        service.bind_image_inputs(profile['text2image']['workflow_id'], 1, {'4': {'text': 'robot'}}, 'request')


def test_importing_the_sample_twice_reuses_existing_workflows(env):
    service = cs.ComfyService(env.store, None)
    first = service.import_image_profile(PROFILE)
    again = service.import_image_profile(PROFILE)
    assert not first['text2image']['reused'] and again['text2image']['reused'] and again['reference']['reused']
    assert again['text2image']['workflow_id'] == first['text2image']['workflow_id']
    assert again['reference']['workflow_id'] == first['reference']['workflow_id']
    assert len(service.library()) == 2


def test_timeout_keeps_pending_run_without_second_submit(setup_image, tmp_path):
    adapter, service, gpu, config, state = setup_image
    state.mode = 'pending'
    with pytest.raises(Unconfirmed, match='/comfyui/runs/'):
        generate(adapter, tmp_path / 'out')
    with pytest.raises(Unconfirmed):
        generate(ComfyImage(config), tmp_path / 'again')
    assert len(state.posts) == 1
    assert gpu.rental(config['comfyui']['image']['rental_id']).status == 'busy'


def test_download_failure_retains_run_and_g11_output(setup_image, tmp_path):
    adapter, service, gpu, config, state = setup_image
    state.mode = 'download_failed'
    with pytest.raises(Unconfirmed, match='output_failed'):
        generate(adapter, tmp_path / 'out')
    run = service.runs()[0]
    assert run.status == 'output_failed' and gpu.unverified_runs(run.rental_id)
    state.mode = 'ok'
    assert service.retry_download(run.run_id).status == 'succeeded'
    generate(adapter, tmp_path / 'recovered')
    assert len(state.posts) == 1


def test_monitor_links_task_run_rental_and_is_read_only(env, setup_image, tmp_path):
    adapter, service, gpu, config, state = setup_image
    generate(adapter, tmp_path / 'out')
    client = TestClient(create_app(config))
    before = len(state.posts)
    html = client.get('/monitor?task=t_0001').text
    run = service.runs()[0]
    assert 'http-equiv="refresh"' in html and run.run_id in html and '/tasks/t_0001?scene=s01' in html
    assert run.rental_id in html and 'succeeded' in html
    page = client.get(f'/comfyui/runs/{run.run_id}').text
    assert 't_0001' in page and '<img' in page
    assert len(state.posts) == before
    assert 'Keyframe trên GPU thuê' in client.get('/settings').text


def test_pipeline_can_commit_comfy_image_as_selected_keyframe(env, setup_image):
    adapter, service, gpu, config, state = setup_image
    task_id = env.new_task()
    env.config = config
    ctx = pipeline.make_ctx(env.store, config, task_id)
    # Dùng plan giả đã có trong bộ test core; ảnh được tạo qua adapter HTTP ComfyUI thật.
    pipeline.run_step(ctx, pipeline.STEPS['plan'])
    ctx = pipeline.make_ctx(env.store, config, task_id)
    result = pipeline.run_item(ctx, pipeline.STEPS['keyframes'], 's01')
    status, item, output = result
    assert status == 'done'
    pipeline._commit(ctx, pipeline.STEPS['keyframes'], item, output)
    candidate = env.td(task_id).selected('s01', 'image')
    assert candidate.provider == 'comfyui' and candidate.width == 1088 and candidate.sha256
    assert candidate.source.startswith('comfyui:cr_')
