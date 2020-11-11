class SettingsGrid {
    constructor(element_id, ws_endpoint) {
        this.element_id = element_id

        this.settings_ws = new WebSocket(ws_endpoint);

        var me = this
   
        this.myAppendGrid = new AppendGrid({
            element: element_id,
            uiFramework: "bootstrap4",
            iconFramework: "fontawesome5",
            initRows: 0,
            columns: [
                {
                    name: "key",
                    display: "Key",
                    events: {
                        change: function(e){
                            me.put_data(e)
                        }
                    }
                },
                {
                    name: "value",
                    display: "Value",
                    events: {
                        change: function(e){
                            me.put_data(e)
                        }
                    }
                },
            ],
            hideButtons: {
                moveUp: true,
                moveDown: true,
                insert: true,
            },
            sectionClasses: {
                table: "table-sm",
                control: "form-control-sm",
                buttonGroup: "btn-group-sm"
            },
            afterRowRemoved: function(caller, rowIndex){
                me.put_data()
            }
        });
    
        this.settings_ws.onmessage = function(event){
            var data = JSON.parse(event.data);
            me.myAppendGrid.load(data)
        }
    }

    put_data(e){
        if(e){
            var check = this.myAppendGrid.getRowValue(this.myAppendGrid.getRowIndex(e.uniqueIndex))
            if(!(check['key'] && check['value']))
                return
        }

        var data = this.myAppendGrid.getAllValue()
        this.settings_ws.send(JSON.stringify({
            "cmd": "put",
            "data": data
        }))
    }
}