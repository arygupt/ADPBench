module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    // BRAM for A matrix (1024 int8) and x vector (64 int8)
    // A is stored row-major
    reg signed [DATA_W-1:0] a_mem [0:ROWS*COLS-1];
    reg signed [DATA_W-1:0] x_mem [0:COLS-1];
    
    // BRAM read ports
    reg signed [DATA_W-1:0] a_rdata;
    reg signed [DATA_W-1:0] x_rdata;
    
    // Counters for input phase
    reg [9:0] a_wr_ptr;  // 0..1023
    reg [5:0] x_wr_ptr;  // 0..63
    
    // Handshake: accept A data when valid & ready, same for x
    wire a_accept = in_a_flat_valid & in_a_flat_ready;
    wire x_accept = in_x_flat_valid & in_x_flat_ready;
    
    // Write A: 64 beats of 16 elements = 1024 elements
    // Write x: 4 beats of 16 elements = 64 elements
    wire a_done = (a_wr_ptr == 10'd1023) & a_accept;
    wire x_done = (x_wr_ptr == 6'd63) & x_accept;
    
    // Unpack A beat into registers for writing
    // Need to write LANES elements per beat
    reg [3:0] a_lane_idx;
    
    // A ready: accept when not all written
    assign in_a_flat_ready = (a_wr_ptr < 10'd1024);
    // x ready: accept when not all written
    assign in_x_flat_ready = (x_wr_ptr < 6'd64);
    
    // Write logic for A
    integer i;
    always @(posedge clk) begin
        if (!rst_n) begin
            a_wr_ptr <= 10'd0;
            a_lane_idx <= 4'd0;
        end else if (a_accept) begin
            for (i = 0; i < LANES; i = i + 1) begin
                if ((a_wr_ptr + i) < ROWS * COLS)
                    a_mem[a_wr_ptr + i] <= in_a_flat[i*DATA_W +: DATA_W];
            end
            a_wr_ptr <= (a_wr_ptr == 10'd1023) ? 10'd0 : a_wr_ptr + 10'd16;
        end
    end
    
    // Write logic for x
    always @(posedge clk) begin
        if (!rst_n) begin
            x_wr_ptr <= 6'd0;
        end else if (x_accept) begin
            for (i = 0; i < LANES; i = i + 1) begin
                if ((x_wr_ptr + i) < COLS)
                    x_mem[x_wr_ptr + i] <= in_x_flat[i*DATA_W +: DATA_W];
            end
            x_wr_ptr <= (x_wr_ptr == 6'd63) ? 6'd0 : x_wr_ptr + 6'd16;
        end
    end
    
    // FSM for computation
    localparam S_IDLE = 2'd0;
    localparam S_LOAD = 2'd1;
    localparam S_COMPUTE = 2'd2;
    localparam S_OUTPUT = 2'd3;
    
    reg [1:0] state;
    reg [3:0] row;      // current row being computed (0..15)
    reg [5:0] col;      // current column index (0..63)
    reg [1:0] load_cnt; // count LANES loads per row
    
    // Accumulator for current row
    reg signed [ACC_W-1:0] acc;
    
    // Precompute: a_addr = row * COLS + col
    wire [9:0] a_addr = row * COLS + col;
    
    // Detect when all inputs are ready
    wire all_loaded = (a_wr_ptr == 10'd1024) & (x_wr_ptr == 6'd64);
    
    // Output valid/ready
    wire out_handshake = out_valid & out_ready;
    
    // BRAM read for A and x
    always @(posedge clk) begin
        a_rdata <= a_mem[a_addr];
        x_rdata <= x_mem[col];
    end
    
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_IDLE;
            row <= 4'd0;
            col <= 6'd0;
            load_cnt <= 2'd0;
            acc <= {ACC_W{1'b0}};
        end else begin
            case (state)
                S_IDLE: begin
                    if (all_loaded) begin
                        state <= S_COMPUTE;
                        row <= 4'd0;
                        col <= 6'd0;
                        acc <= {ACC_W{1'b0}};
                    end
                end
                
                S_COMPUTE: begin
                    // Pipeline: col + 2 latency for BRAM read
                    // First valid result at col = 2
                    if (col >= 6'd2) begin
                        acc <= acc + $signed(a_rdata) * $signed(x_rdata);
                    end
                    if (col == 6'd1) begin
                        acc <= $signed(a_rdata) * $signed(x_rdata);
                    end
                    if (col == 6'd0) begin
                        // Initialize for first multiply
                    end
                    
                    if (col == COLS - 1 + 2) begin
                        // Last column result ready
                        // We need to store the final accumulated value
                        // Actually, need to handle col overflow
                    end
                    
                    col <= col + 6'd1;
                    
                    // At col = COLS + 1, we have the last multiply result
                    if (col == COLS + 1) begin
                        state <= S_OUTPUT;
                        col <= 6'd0;
                    end
                end
                
                S_OUTPUT: begin
                    if (out_handshake) begin
                        row <= row + 4'd1;
                        if (row == ROWS - 1) begin
                            state <= S_IDLE;
                            row <= 4'd0;
                        end else begin
                            state <= S_COMPUTE;
                            col <= 6'd0;
                            acc <= {ACC_W{1'b0}};
                        end
                    end
                end
                
                default: state <= S_IDLE;
            endcase
        end
    end
    
    // Output valid
    assign out_valid = (state == S_OUTPUT);
    assign out_c = acc;

endmodule
