/* v2 only; also loadable by Node for the dependency-free checks. */
(function (root) {
  'use strict';
  const clone = value => JSON.parse(JSON.stringify(value));
  const writable = row => row && row.source === 'server';
  function normalize(clientData, rules) {
    const clients = clientData.map(c => ({name:c.client_name, online:c.status === 'online', version:c.version || ''}));
    const forward = clientData.flatMap(c => (c.config_list || []).map(r => ({
      id:JSON.stringify([c.client_name,r.name]), client:c.client_name, name:r.name,
      remote:r.remote_port, ip:r.local_ip, port:r.local_port, protocol:r.protocol || 'tcp',
      speed:r.speed_limit === undefined ? 0 : r.speed_limit,
      source:(c.can_delete_names || []).includes(r.name) ? 'server' : 'client'
    })));
    const c2c = rules.map(r => {
      [r.source_client,r.target_client].forEach(name => {
        if (name && !clients.some(c => c.name === name)) clients.push({name,online:false,version:''});
      });
      return {id:r.name, name:r.name, client:r.source_client, target:r.target_client,
        ip:r.local_ip || '127.0.0.1', port:r.local_port, protocol:r.protocol || 'tcp',
        speed:r.speed_limit === undefined ? 0 : r.speed_limit, enabled:r.enabled !== false,
        p2p_enabled:r.p2p_enabled === undefined ? true : r.p2p_enabled,
        mode:r.target_ip && r.target_port ? 'direct' : 'service',
        targetIp:r.target_ip || '', targetPort:r.target_port, service:r.target_service || ''};
    });
    clients.sort((a,b) => a.name.localeCompare(b.name));
    return {clients,forward,c2c};
  }
  function matchRule(rule, type, query, forward) {
    query = query.trim().toLowerCase();
    const endpoints = type === 'forward'
      ? [{label:'服务端监听',ip:'服务端',port:rule.remote},{label:'本地目标',ip:rule.ip,port:rule.port}]
      : [{label:'源端监听',ip:rule.ip,port:rule.port}];
    if (type === 'c2c') {
      if (rule.mode === 'service') {
        const service = forward.find(r => r.client === rule.target && r.name === rule.service && r.protocol === rule.protocol);
        if (service) {
          endpoints.push({label:'目标地址（通过服务 '+rule.service+'）',ip:service.ip,port:service.port});
          endpoints.push({label:'服务端监听（引用服务 '+rule.service+'）',ip:'服务端',port:service.remote});
        }
      } else endpoints.push({label:'目标地址',ip:rule.targetIp,port:rule.targetPort});
    }
    const numeric = /^\d+$/.test(query);
    const matches = endpoints.filter(e => !query || (numeric ? Number(query) === Number(e.port) : (e.ip+':'+e.port).toLowerCase().includes(query)))
      .map(e => e.label+' · '+e.ip+':'+e.port);
    if (query) {
      for (const [label,value] of [['名称',rule.name],['源客户端',rule.client],['目标客户端',rule.target],['目标服务',rule.service]]) {
        if (value && value.toLowerCase().includes(query)) matches.push(label+'匹配 · '+value);
      }
    }
    return matches;
  }
  function payload(form, type, editing) {
    const data = {name:form.name.trim(),local_ip:form.ip.trim(),local_port:form.port,
      protocol:form.protocol || 'tcp',speed_limit:form.speed,is_edit:!!editing};
    if (type === 'forward') Object.assign(data,{client_name:form.client,remote_port:form.remote});
    else {
      Object.assign(data,{source_client:form.client,target_client:form.target,enabled:form.enabled,
        p2p_enabled:form.p2p_enabled === undefined ? true : form.p2p_enabled});
      if (form.mode === 'direct') Object.assign(data,{target_ip:form.targetIp.trim(),target_port:form.targetPort});
      else data.target_service = form.service;
    }
    return data;
  }
  function checked(response, reload) {
    if (response.status === 401 || (response.data && response.data.code === 401)) {
      reload(); throw new Error('登录已过期，请重新登录');
    }
    if (response.status !== 200 || !response.data || response.data.code !== 200) {
      throw new Error(response.data && response.data.msg || '请求失败');
    }
    return response.data;
  }
  function options(http, base, reload) {
    base = base.replace(/\/+$/, '');
    async function request(method, path, data) {
      try { return checked(await http[method](base + path, data), reload); }
      catch (error) {
        if (error.response && error.response.status === 401) reload();
        throw error;
      }
    }
    return {
      el:'#app', delimiters:['{[',']}'],
      data() { return {
        clients:[],forward:[],c2c:[],serverVersion:'',selectedClient:'all',clientSearch:'',clientStatus:'all',
        activeTab:'forward',search:'',protocol:'all',enabledFilter:'all',searchScope:'current',localView:null,
        page:1,pageSize:20,editorVisible:false,editingId:null,editorType:'forward',form:{},
        refreshing:false,saving:false,refreshPromise:null,intervalTask:null,destroyed:false,error:'',lastUpdated:''
      }; },
      computed:{
        visibleClients() {
          const query = this.clientSearch.trim().toLowerCase();
          return this.clients.filter(c => c.name.toLowerCase().includes(query) &&
            (this.clientStatus === 'all' || c.online === (this.clientStatus === 'online')));
        },
        currentClient() { return this.clients.find(c => c.name === this.selectedClient); },
        scopedForward() { return this.forward.filter(r => this.selectedClient === 'all' || r.client === this.selectedClient); },
        scopedC2c() { return this.c2c.filter(r => this.selectedClient === 'all' || r.client === this.selectedClient); },
        filteredRows() {
          const query = this.search.trim().toLowerCase();
          if (this.searchScope === 'global') return [{type:'forward',rows:this.forward},{type:'c2c',rows:this.c2c}].flatMap(group =>
            group.rows.map(rule => ({key:group.type+rule.id,type:group.type,rule,matches:matchRule(rule,group.type,query,this.forward)}))
              .filter(r => r.matches.length && (this.protocol === 'all' || r.rule.protocol === this.protocol)));
          return (this.activeTab === 'forward' ? this.scopedForward : this.scopedC2c).filter(r =>
            matchRule(r,this.activeTab,query,this.forward).length && (this.protocol === 'all' || r.protocol === this.protocol) &&
            (this.activeTab !== 'c2c' || this.enabledFilter === 'all' || r.enabled === (this.enabledFilter === 'yes')));
        },
        pageRows() { return this.filteredRows.slice((this.page-1)*this.pageSize,this.page*this.pageSize); },
        targetClients() { return this.clients.filter(c => c.name !== this.form.client); },
        targetServices() { return this.forward.filter(r => r.client === this.form.target && r.protocol === this.form.protocol); },
        formRules() {
          const text = label => [{required:true,whitespace:true,message:'请填写'+label,trigger:'blur'}];
          const port = min => [{required:true,type:'integer',min,max:65535,message:'请输入 '+min+'–65535 的端口',trigger:'blur'}];
          return {name:text('规则名称'),client:[{required:true,message:'请选择客户端',trigger:'change'}],ip:text('本地 IP'),
            port:port(1),remote:port(1000),target:[{required:true,message:'请选择目标客户端',trigger:'change'}],
            targetIp:text('目标 IP'),targetPort:port(1),service:[{required:true,message:'请选择目标服务',trigger:'change'}],
            speed:[{required:true,type:'number',min:0,message:'请输入大于或等于 0 的限速',trigger:'blur'}]};
        }
      },
      watch:{'filteredRows.length'() { this.page = Math.min(this.page,Math.max(1,Math.ceil(this.filteredRows.length/this.pageSize))); }},
      mounted() { this.refresh(); this.intervalTask = setInterval(() => { if (!this.saving) this.refresh(); },6000); },
      beforeDestroy() { this.destroyed = true; clearInterval(this.intervalTask); },
      methods:{
        async refresh() {
          if (this.destroyed || this.saving) return;
          if (this.refreshing) return this.refreshPromise;
          this.refreshing = true;
          this.refreshPromise = (async () => {
            try {
              const [clients,rules] = await Promise.all([request('get','/api'),request('get','/c2c_rules')]);
              if (!Array.isArray(clients.data) || !Array.isArray(rules.data)) throw new Error('服务器返回数据无效');
              const next = normalize(clients.data,rules.data);
              if (!this.destroyed) { Object.assign(this,next); this.serverVersion = clients.server_version || ''; this.error = ''; this.lastUpdated = new Date().toLocaleTimeString(); }
            } catch (error) { if (!this.destroyed) this.error = error.message || '刷新失败'; }
            finally { this.refreshing = false; this.refreshPromise = null; }
          })();
          return this.refreshPromise;
        },
        changeScope(scope) {
          if (scope === this.searchScope) return;
          if (scope === 'global') {
            this.localView = {selectedClient:this.selectedClient,activeTab:this.activeTab,search:this.search,protocol:this.protocol,enabledFilter:this.enabledFilter,page:this.page,pageSize:this.pageSize};
            this.searchScope = scope; this.protocol = 'all'; this.page = 1;
          } else { this.searchScope = scope; if (this.localView) Object.assign(this,this.localView); this.localView = null; }
        },
        selectClient(name) { this.changeScope('current'); this.selectedClient = name; this.page = 1; },
        goToClient(name,type) { this.selectClient(name); this.activeTab = type; this.search = ''; this.clearFilters(); },
        isOnline(name) { return this.clients.some(c => c.name === name && c.online); },
        filterChanged(filters) { this.enabledFilter = (filters.enabled || [])[0] || 'all'; this.page = 1; },
        clearFilters() { this.protocol = this.enabledFilter = 'all'; this.page = 1; },
        clearFormValidation() { this.$nextTick(() => { if (this.$refs.ruleForm) this.$refs.ruleForm.clearValidate(); }); },
        sourceClientChanged() { if (this.form.target === this.form.client) { this.form.target = ''; this.form.service = ''; } },
        closeEditor(done) { if (!this.saving) done(); },
        openEditor(row,type) {
          if (this.saving) return;
          type = type || this.activeTab;
          if (row && type === 'forward' && !writable(row)) { this.$message.info('客户端配置只读，请在客户端修改。'); return; }
          this.editorType = type; this.editingId = row ? row.id : null;
          this.form = Object.assign({name:'',client:this.selectedClient === 'all' ? '' : this.selectedClient,remote:6000,ip:'127.0.0.1',port:22,
            protocol:'tcp',speed:0,target:'',mode:'direct',targetIp:'127.0.0.1',targetPort:22,service:'',enabled:true,p2p_enabled:true},row ? clone(row) : {});
          this.editorVisible = true; this.clearFormValidation();
        },
        async commit(method) {
          if (this.saving) return;
          this.saving = true;
          try {
            // Drain an older read before writing so its response cannot overwrite the post-save refresh.
            if (this.refreshPromise) await this.refreshPromise;
            const type = this.editorType;
            if (type === 'forward' && this.editingId && !this.forward.some(r => r.id === this.editingId && writable(r))) throw new Error('配置已不存在或只读，请刷新后重试');
            const path = type === 'forward' ? '/api' : '/c2c_rules';
            const data = method === 'delete' ? {params:type === 'forward' ? {client_name:this.form.client,name:this.form.name} : {name:this.form.name}}
              : payload(this.form,type,this.editingId);
            await request(method,path,data);
            if (this.destroyed) return;
            this.editorVisible = false; this.$message.success('操作成功，相关客户端将重新连接');
          } catch (error) { this.$message.error(error.message || '操作失败'); return; }
          finally { this.saving = false; }
          if (!this.destroyed) await this.refresh();
        },
        saveRule() {
          if (this.saving) return;
          this.$refs.ruleForm.validate(valid => {
            if (!valid || this.saving) return;
            if (!Number.isFinite(this.form.speed) || this.form.speed < 0) return this.$message.error('限速必须大于或等于 0');
            if (this.editorType === 'c2c' && this.form.target === this.form.client) return this.$message.error('源客户端和目标客户端不能相同');
            // An existing unresolved service is kept editable when its target is offline.
            if (this.editorType === 'c2c' && this.form.mode === 'service' && !this.targetServices.some(r => r.name === this.form.service)) {
              const old = this.c2c.find(r => r.id === this.editingId);
              if (!old || old.service !== this.form.service || old.target !== this.form.target || old.protocol !== this.form.protocol) return this.$message.error('请选择匹配协议的目标服务');
            }
            this.commit('post');
          });
        },
        async deleteRule() {
          if (this.saving) return;
          // Lock before confirmation as well, so repeated clicks cannot open multiple delete flows.
          this.saving = true;
          try { await this.$confirm('确定删除「'+this.form.name+'」？相关客户端将重新连接。','删除规则',{type:'warning',confirmButtonText:'删除',cancelButtonText:'取消'}); }
          catch (_) { this.saving = false; return; }
          this.saving = false; return this.commit('delete');
        }
      }
    };
  }
  const api = {normalize,matchRule,payload,writable,checked,options};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else { root.ProxyntAdminV2 = api; }
})(typeof window !== 'undefined' ? window : globalThis);
