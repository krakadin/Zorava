import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import worktree_mcp as mcp


class WorktreeMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-worker-mcp-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'worktree'
        self.root.mkdir()
        self.job = self.base / 'job'
        self.job.mkdir()
        self.secret = self.root / '.env'
        self.secret.write_text('FAKE_SECRET')
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'app.py').write_text('value = 1\n')
        (self.root / 'escape').symlink_to(self.job, target_is_directory=True)

    def test_tools_are_scoped_and_editor_can_change_source(self):
        listed=mcp.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'},self.root,self.job)
        names={tool['name'] for tool in listed['result']['tools']}
        self.assertEqual(names,{'get_task','list_files','read_file','search_text','write_file','replace_in_file'})
        response=mcp.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'replace_in_file','arguments':{'path':'src/app.py','old_text':'value = 1','new_text':'value = 2'}}},self.root,self.job)
        self.assertFalse(response['result']['isError'])
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')
        self.assertEqual((self.job/'sentinel').exists(),False)

    def test_rejects_traversal_absolute_and_symlink_escape(self):
        for path in ('../job/sentinel','/etc/passwd','escape/sentinel'):
            with self.subTest(path=path):
                with self.assertRaises(mcp.ToolError):
                    mcp.safe_target(self.root,path,allow_missing=True)

    def test_sensitive_files_are_not_listed_or_read(self):
        paths=mcp.list_files(self.root)
        self.assertNotIn('.env',paths)
        with self.assertRaises(mcp.ToolError):
            mcp.read_text(self.root,'.env')
        self.assertNotIn('FAKE_SECRET',' '.join(paths))

    def test_task_tool_returns_parent_request_not_arbitrary_path(self):
        (self.job/'request.json').write_text('{"task":"inspect this"}')
        response=mcp.handle({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{
            'name':'get_task','arguments':{}}},self.root,self.job)
        self.assertIn('inspect this',response['result']['content'][0]['text'])

    def test_write_creates_only_under_root_and_rejects_sensitive_names(self):
        mcp.write_text(self.root,'new/note.txt','hello')
        self.assertEqual((self.root/'new/note.txt').read_text(),'hello')
        for path in ('../job/leak','new/.env','escape/new.txt'):
            with self.subTest(path=path), self.assertRaises(mcp.ToolError):
                mcp.write_text(self.root,path,'no')


if __name__ == '__main__':
    unittest.main()
