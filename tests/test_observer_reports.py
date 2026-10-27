"""Direct Observer report/answer persistence and MCP ownership contracts."""
import asyncio
import base64
from types import SimpleNamespace
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from claude_launcher.daemon.observer_reports import Reports
from claude_launcher import observer_mcp

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

@pytest.fixture
def service(tmp_path):
    sent=[]
    async def deliver(text):
        sent.append(text)
        return True
    session=SimpleNamespace(sdef=SimpleNamespace(name='s1'),exited=False,deliver=deliver)
    return Reports(tmp_path,SimpleNamespace(list=lambda:[session])),session,sent


def test_report_idempotency_and_restart(service):
    reports,session,sent=service
    body={'text':'12 tests passed','state':'done','request_id':'build-1'}
    event=reports.publish('s1',body)
    assert reports.publish('s1',body)['id']==event['id']
    with pytest.raises(web.HTTPConflict):
        reports.publish('s1',{**body,'text':'different'})
    restored=Reports(reports.root.parent,reports.manager)
    assert restored.rows('s1')[0]['text']=='12 tests passed'
    with pytest.raises(web.HTTPNotFound):
        reports.publish('s2',body)


def test_answers_are_durable_idempotent_and_not_approvals(service):
    reports,session,sent=service
    event=reports.publish('s1',{'text':'Choose environment','question':True,'choices':['staging','production']})
    async def check():
        result=await reports.answer('s1',event['id'],{'text':'staging with extra constraints'})
        assert result['answer']['text']=='staging with extra constraints'
        assert result['delivery']=='sent' and not result['needs_action']
        await reports.answer('s1',event['id'],{'text':'staging with extra constraints'})
        assert len(sent)==1
        with pytest.raises(web.HTTPConflict):
            await reports.answer('s1',event['id'],{'text':'production'})
    asyncio.run(check())
    restored=Reports(reports.root.parent,reports.manager)
    assert restored.find('s1',event['id'])['answer']['text'].startswith('staging')


def test_uncertain_delivery_is_not_repeated(service):
    reports,session,sent=service
    async def fail(text):
        sent.append(text)
        raise OSError('delivery interrupted')
    session.deliver=fail
    event=reports.publish('s1',{'text':'Question','question':True})
    async def check():
        assert (await reports.answer('s1',event['id'],{'text':'yes'}))['delivery']=='unknown'
        await reports.answer('s1',event['id'],{'text':'yes'})
        assert len(sent)==1
    asyncio.run(check())


def test_concurrent_answer_only_delivers_once(service):
    reports,session,sent=service
    event=reports.publish('s1',{'text':'Question','question':True})
    async def check():
        entered=asyncio.Event();release=asyncio.Event()
        async def deliver(text):
            sent.append(text);entered.set();await release.wait();return True
        session.deliver=deliver
        first=asyncio.create_task(reports.answer('s1',event['id'],{'text':'yes'}))
        await entered.wait()
        second=await reports.answer('s1',event['id'],{'text':'yes'})
        assert second['delivery']=='delivering'
        release.set();await first
        assert len(sent)==1
    asyncio.run(check())


def test_pending_questions_survive_report_retention(service):
    reports,_,_=service
    first=reports.publish('s1',{'text':'Question','question':True})
    for i in range(205):
        reports.publish('s1',{'text':str(i)})
    assert reports.find('s1',first['id'])['question']
    assert len(reports.rows('s1'))==201


def test_images_and_api_contract(service):
    reports,_,_=service
    async def check():
        app=web.Application();reports.install(app)
        async with TestClient(TestServer(app)) as client:
            response=await client.post('/api/observer/s1/images',json={'data':base64.b64encode(PNG).decode()})
            assert response.status==200
            image_id=(await response.json())['id']
            response=await client.post('/api/observer/s1/reports',json={'text':'Screenshot','attachments':[image_id]})
            event=await response.json();assert response.status==200
            image=await client.get(f'/api/observer/s1/reports/{event["id"]}/images/{image_id}')
            assert await image.read()==PNG and image.content_type=='image/png'
            assert (await client.get(f'/api/observer/s1/reports/{event["id"]}/images/{"0"*64}')).status==404
            assert (await client.post('/api/observer/s1/images',json={'data':base64.b64encode(b'<svg/>').decode()})).status==400
            assert (await client.post('/api/observer/s1/reports',json={'text':'x','attachments':['../file']})).status==400
            assert (await client.post(f'/api/observer/s1/reports/{event["id"]}/answer',json={'text':'yes'})).status==400
    asyncio.run(check())


def test_a_locked_index_does_not_turn_an_accepted_answer_into_a_500(service, monkeypatch, caplog):
    """The answer is in the cache before save(); a locked index must not undo it.

    ``Reports.answer`` writes the human's answer into ``self.cache`` and only
    then calls ``save()``. ``save`` mirrors to the search index first and, with
    no guard, a ``sqlite3.OperationalError`` there would escape as a 500 -- a
    person reading that believes their answer was not stored, while the daemon
    has already accepted it. The index is derived and the file write is what
    makes the answer durable, so the mirror must not decide the answer's fate.

    Mutation: removing the try/except in ``Reports.save`` makes this red and
    leaves the other tests in this module green -- they stub no index failure.
    """
    import sqlite3
    from claude_launcher.daemon import search_records

    reports, session, sent = service

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    # Publish with a working index: the stub goes in after the question exists,
    # so the red this test produces lands on `answer` -- the call the issue is
    # about -- and not on the publish that merely seeded it.
    event = reports.publish('s1', {'text': 'Choose environment', 'question': True,
                                  'choices': ['staging', 'production']})
    monkeypatch.setattr(search_records, "remember", locked)

    async def check():
        return await reports.answer('s1', event['id'], {'text': 'staging'})

    result = asyncio.run(check())
    assert result['answer']['text'] == 'staging'
    assert "could not index the observer reports for s1" in caplog.text
    # The file is what survives a restart, so assert on a fresh load rather
    # than on the cache this call wrote.
    restored = Reports(reports.root.parent, reports.manager)
    assert restored.find('s1', event['id'])['answer']['text'] == 'staging'


def test_mcp_uses_calling_session_and_uploads_images(tmp_path,monkeypatch):
    monkeypatch.setenv('CLAUNCH_SESSION','s1')
    calls=[]
    class Client:
        def post(self,path,body):
            calls.append((path,body));return {'id':'image'} if path.endswith('/images') else body
        def get(self,path):
            calls.append((path,None));return {'reports':[{'id':'event','request_id':'request'}]}
    monkeypatch.setattr(observer_mcp.daemon_client,'connect_with_diagnosis',lambda:(Client(),None))
    image=tmp_path/'capture.png';image.write_bytes(PNG)
    result=observer_mcp.call_tool('observer_ask',{'session':'other','text':'Choose','request_id':'request','images':[str(image)],'choices':['a','b']})
    assert all('/s1/' in path for path,body in calls)
    assert result['question'] and result['attachments']==['image']
    assert observer_mcp.call_tool('observer_requests',{'request_id':'request'})['reports'][0]['id']=='event'
    monkeypatch.delenv('CLAUNCH_SESSION')
    with pytest.raises(observer_mcp.ObserverMcpError):
        observer_mcp.call_tool('observer_report',{'text':'x'})
